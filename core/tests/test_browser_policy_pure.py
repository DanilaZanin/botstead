"""Browser hardening, pure logic: URL policy, one masking function, risk classes of browser actions,
runner parsers and the bot-side MCP tool. No Postgres."""
import json
import types

import httpx
import pytest

from bothub import mcp_server as srv
from bothub.browser_control import (
    MASK, BrowserEventMasker, mask_browser_args, mask_browser_result, mask_browser_text, mask_url,
    url_forbidden, url_origin)
from bothub.risk import classify, decide, forbidden_reason, permission_class, remember_rule, rule_matches
from bothub.runner import ClaudeRunner, CodexRunner

pytestmark = pytest.mark.pure
BROWSER = "mcp__bothub__browser"
SECRET = "hunter2-Very-Private"


# --- URL policy ---------------------------------------------------------------

ATTACKS = [
    "file:///home/bot/.claude/.credentials.json",
    "http://169.254.169.254/latest/meta-data/",
    "http://core:8080/gateway/",
    "chrome://settings/passwords",
    "view-source:file:///proc/self/environ",
    "chrome-extension://abcdef/popup.html", "devtools://devtools/bundled/inspector.html",
    "javascript:alert(document.cookie)", "data:text/html,<script>1</script>", "blob:https://example.com/1",
    "http://localhost:8000/", "http://localhost./", "http://app.localhost/", "http://127.0.0.1/", "http://127.1/",
    "http://0x7f.0.0.1/", "http://0177.0.0.1/", "http://2130706433/", "http://0.0.0.0/",
    "http://[::1]/", "http://[::ffff:127.0.0.1]/", "http://[fe80::1]/", "http://[fd12:3456::1]/",
    "http://[2002:7f00:1::]/", "http://[64:ff9b::7f00:1]/",
    "http://10.1.2.3/", "http://172.16.0.9:8080/", "http://192.168.0.1/", "http://100.64.1.1/",
    "http://metadata.google.internal/", "http://gateway/", "http://core/", "http://printer.local/",
    "http://127.0.0.1.nip.io/", "http://１２７．０．０．１/",
    "http://example.com@127.0.0.1/", "http://127.0.0.1\\@example.com/", "http://127.0.0.1 .example.com/",
    "http://1.2.3.4.5/", "example.com", "//example.com/", "http:example.com", "", None, 5,
]


@pytest.mark.parametrize("url", ATTACKS)
def test_attack_urls_are_forbidden(url):
    assert url_forbidden(url)
    assert url_origin(url) is None


@pytest.mark.parametrize("url", ["https://example.com/", "http://example.org:8080/a?b=1#c", "https://8.8.8.8/",
                                 "https://ya.ru:8443/x", "https://пример.рф/", "about:blank"])
def test_public_urls_and_blank_page_are_allowed(url):
    assert url_forbidden(url) is None


def test_configured_hosts_are_forbidden(monkeypatch):
    monkeypatch.setenv("BOTHUB_BROWSER_DENY_HOSTS", "bots.example.net, other.example.net")
    monkeypatch.setenv("BOTHUB_URL", "https://hub.example.net/bots")
    for host in ("bots.example.net", "other.example.net", "hub.example.net"):
        assert url_forbidden(f"https://{host}/") == "internal_host"
    assert url_forbidden("https://example.com/") is None


def test_origin_of_allowed_url():
    assert url_origin("https://Example.com:8443/a?b#c") == "https://example.com:8443"
    assert url_origin("about:blank") is None


# --- Masking: one function for events, tool_result and approvals --------------

def test_mask_url_keeps_scheme_host_path_only():
    assert mask_url("https://user:pw@example.com:8443/a/b?token=1#frag") == "https://example.com:8443/a/b"
    assert mask_url("https://example.com") == "https://example.com/"
    assert mask_url("about:blank") == "about:blank"
    assert mask_url("file:///home/bot/.claude/.credentials.json") == MASK
    assert mask_url("javascript:alert(1)") == MASK
    assert mask_url(None) is None and mask_url("") == ""


def test_mask_args_replaces_typed_text_and_reduces_url():
    args = {"action": "fill", "target": "e5", "value": SECRET, "text": SECRET, "url": "https://a.com/p?q=" + SECRET}
    masked = mask_browser_args(args)
    assert masked == {"action": "fill", "target": "e5", "value": MASK, "text": MASK, "url": "https://a.com/p"}
    assert SECRET not in json.dumps(masked)
    assert args["value"] == SECRET, "the input is not mutated"
    assert mask_browser_args(masked) == masked, "idempotent"
    assert mask_browser_args({"action": "click", "value": ""}) == {"action": "click", "value": ""}


def test_mask_text_reduces_every_url():
    text = f"Page URL: https://a.com/x?token={SECRET}. See (http://b.org/y?z=1), file:///etc/passwd"
    masked = mask_browser_text(text)
    assert SECRET not in masked and "z=1" not in masked and "/etc/passwd" not in masked
    assert "https://a.com/x." in masked and "http://b.org/y)" in masked


def test_result_of_fill_is_dropped_whole_and_unknown_action_too():
    assert mask_browser_result("fill", f"await page.fill('{SECRET}')") == MASK
    assert mask_browser_result(None, "anything") == MASK
    assert mask_browser_result("click", "") == ""
    assert mask_browser_result("click", "go https://a.com/?s=1") == "go https://a.com/"


def test_masker_follows_call_id_from_call_to_result():
    masker = BrowserEventMasker()
    call = masker.call({"call_id": "c1", "tool": BROWSER, "args": {"action": "fill", "target": "e5", "value": SECRET}})
    assert call["args"]["value"] == MASK
    result = masker.result({"call_id": "c1", "ok": True, "summary": f"typed {SECRET}"})
    assert result["summary"] == MASK
    other = {"call_id": "c2", "tool": "Bash", "args": {"command": "echo hi"}}
    assert masker.call(other) is other
    plain = {"call_id": "c2", "ok": True, "summary": "hi https://a.com/?x=1"}
    assert masker.result(plain) is plain, "only browser calls are touched"
    nav = masker.call({"call_id": "c3", "tool": BROWSER, "args": {"action": "navigate", "url": "https://a.com/p?k=" + SECRET}})
    assert nav["args"]["url"] == "https://a.com/p"
    assert masker.result({"call_id": "c3", "summary": f"https://a.com/p?k={SECRET}"})["summary"] == "https://a.com/p"


def _claude_messages(args, result_text):
    return [
        {"type": "assistant", "session_id": "s", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": BROWSER, "input": args}]}},
        {"type": "user", "session_id": "s", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": result_text}]}},
    ]


def test_claude_parser_masks_browser_fill_in_call_and_result():
    runner = ClaudeRunner()
    events = [e for m in _claude_messages({"action": "fill", "target": "e5", "value": SECRET},
                                          f"await page.getByRole('textbox').fill('{SECRET}')") for e in runner.parse(m)]
    assert [e.kind for e in events] == ["tool_call", "tool_result"]
    assert events[0].payload["args"] == {"action": "fill", "target": "e5", "value": MASK}
    assert events[1].payload["summary"] == MASK
    assert SECRET not in json.dumps([e.payload for e in events])


def test_claude_parser_masks_navigate_url_and_page_urls():
    runner = ClaudeRunner()
    events = [e for m in _claude_messages({"action": "navigate", "url": "https://a.com/in?t=" + SECRET},
                                          "- Page URL: https://a.com/in?t=" + SECRET) for e in runner.parse(m)]
    assert events[0].payload["args"]["url"] == "https://a.com/in"
    assert events[1].payload["summary"] == "- Page URL: https://a.com/in"


def test_codex_parser_masks_browser_call_and_result():
    runner = CodexRunner()
    started = runner.parse({"type": "item.started", "thread_id": "x", "item": {
        "id": "i1", "type": "mcp_tool_call", "server": "bothub", "tool": "browser",
        "arguments": {"action": "fill", "target": "e5", "text": SECRET}}})
    done = runner.parse({"type": "item.completed", "thread_id": "x", "item": {
        "id": "i1", "type": "mcp_tool_call", "server": "bothub", "tool": "browser", "status": "completed",
        "result": {"content": [{"text": f"fill('{SECRET}')"}]}}})
    assert started[0].payload["tool"] == "bothub.browser"
    assert started[0].payload["args"]["text"] == MASK
    assert done[0].payload["summary"] == MASK
    assert done[0].payload["data"]["result"] == MASK and done[0].payload["data"]["server"] == "bothub"
    assert SECRET not in json.dumps([e.payload for e in started + done])


def test_codex_result_without_seen_call_is_masked_by_tool_name():
    done = CodexRunner().parse({"type": "item.completed", "thread_id": "x", "item": {
        "id": "late", "type": "mcp_tool_call", "server": "bothub", "tool": "browser", "status": "completed",
        "arguments": {"action": "fill"}, "result": SECRET}})
    assert SECRET not in json.dumps(done[0].payload)


def test_other_tools_are_not_masked_by_parsers():
    runner = ClaudeRunner()
    events = [e for m in [
        {"type": "assistant", "session_id": "s", "message": {"content": [
            {"type": "tool_use", "id": "t9", "name": "Bash", "input": {"command": "echo https://a.com/?x=1"}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t9", "content": "https://a.com/?x=1"}]}},
    ] for e in runner.parse(m)]
    assert events[0].payload["args"] == {"command": "echo https://a.com/?x=1"}
    assert events[1].payload["summary"] == "https://a.com/?x=1"


# --- Risk classes of browser actions ------------------------------------------

def _decide(args, rules=()):
    return decide({"auto_allow": list(rules), "mac_full_control": True}, BROWSER, args)


@pytest.mark.parametrize("action", ["snapshot", "screenshot"])
def test_reading_the_page_needs_no_question(action):
    assert _decide({"action": action}) == ("other", True)
    assert permission_class(BROWSER, {"action": action}) == "safe"


@pytest.mark.parametrize("url", ATTACKS[:12])
def test_forbidden_navigation_is_refused_and_never_asked(url):
    args = {"action": "navigate", "url": url}
    assert forbidden_reason(BROWSER, args)
    assert _decide(args)[1] is False
    assert permission_class(BROWSER, args) == "blocked"
    assert remember_rule(BROWSER, args) is None


def test_forbidden_reason_only_for_browser_navigate():
    assert forbidden_reason(BROWSER, {"action": "click", "target": "e1"}) is None
    assert forbidden_reason("WebFetch", {"url": "http://127.0.0.1/"}) is None
    assert forbidden_reason(BROWSER, {"action": "navigate", "url": "https://example.com/"}) is None


def test_navigation_to_public_site_asks_and_is_remembered_by_origin():
    args = {"action": "navigate", "url": "https://example.com/start?x=1"}
    assert _decide(args) == ("other", False)
    rule = remember_rule(BROWSER, args)
    assert rule == {"tool": BROWSER, "scope": "browser_origin", "match": {"action": "navigate", "origin": "https://example.com"}}
    assert _decide(args, [rule])[1] is True
    assert _decide({"action": "navigate", "url": "https://example.com/other"}, [rule])[1] is True
    assert _decide({"action": "navigate", "url": "https://example.org/"}, [rule])[1] is False
    assert _decide({"action": "navigate", "url": "http://example.com/"}, [rule])[1] is False, "origin includes the scheme"
    assert _decide({"action": "click", "target": "e1", "element": "Next", "origin": "https://example.com"}, [rule])[1] is False
    assert _decide({"action": "navigate", "url": "http://127.0.0.1/"}, [rule])[1] is False


def test_click_pay_now_always_asks_even_with_a_rule():
    args = {"action": "click", "target": "e12", "element": "Pay now", "origin": "https://shop.example.com"}
    assert classify(BROWSER, args) == "pay"
    assert _decide(args) == ("pay", False)
    assert remember_rule(BROWSER, args) is None
    forged = {"tool": BROWSER, "scope": "browser_origin", "match": {"action": "click", "origin": "https://shop.example.com"}}
    assert _decide(args, [forged])[1] is False
    assert _decide(args, [{"tool": BROWSER}])[1] is False


@pytest.mark.parametrize("args,label", [
    ({"action": "click", "target": "e1", "element": "Delete account"}, "delete"),
    ({"action": "click", "target": "e1", "page_label": "button Удалить"}, "delete"),
    ({"action": "click", "target": "e1", "element": "Оплатить заказ"}, "pay"),
    ({"action": "click", "target": "e1", "element": "Checkout"}, "pay"),
    ({"action": "click", "target": "e1", "element": "Sign in"}, "login"),
    ({"action": "fill", "target": "e2", "element": "Password", "value": "x"}, "login"),
    ({"action": "fill", "target": "e2", "page_label": "textbox Пароль", "value": "x"}, "login"),
    ({"action": "fill", "target": "e2", "element": "One-time code", "value": "x"}, "login"),
    ({"action": "fill", "target": "e2", "element": "Card number", "value": "x"}, "pay"),
    ({"action": "fill", "target": "e2", "element": "CVV", "value": "x"}, "pay"),
])
def test_sensitive_fields_and_buttons_always_ask(args, label):
    assert classify(BROWSER, args) == label
    assert _decide(args) == (label, False)
    assert remember_rule(BROWSER, args) is None


def test_label_from_the_page_beats_a_harmless_description():
    args = {"action": "click", "target": "e12", "element": "Next", "page_label": "button Pay now"}
    assert classify(BROWSER, args) == "pay"


def test_ordinary_click_and_fill_are_remembered_by_action_and_origin():
    origin = "https://example.com"
    click = {"action": "click", "target": "e3", "element": "Next page", "origin": origin}
    fill = {"action": "fill", "target": "e4", "element": "Search", "value": "cats", "origin": origin}
    assert _decide(click) == ("other", False) and _decide(fill) == ("other", False)
    click_rule, fill_rule = remember_rule(BROWSER, click), remember_rule(BROWSER, fill)
    assert click_rule["match"] == {"action": "click", "origin": origin}
    assert fill_rule["match"] == {"action": "fill", "origin": origin}
    assert _decide(click, [click_rule])[1] is True
    assert _decide(fill, [fill_rule])[1] is True
    assert _decide(fill, [click_rule])[1] is False
    assert _decide(click | {"origin": "https://evil.example"}, [click_rule])[1] is False
    assert rule_matches(click_rule, BROWSER, click | {"target": "e99"}), "the rule is about action and origin, not the ref"


def test_bare_refs_and_unknown_origin_cannot_be_remembered():
    assert remember_rule(BROWSER, {"action": "click", "target": "e12", "origin": "https://example.com"}) is None
    assert remember_rule(BROWSER, {"action": "click", "target": "e12", "element": "e12", "origin": "https://example.com"}) is None
    assert remember_rule(BROWSER, {"action": "click", "target": "e1", "element": "Next"}) is None
    assert remember_rule(BROWSER, {"action": "click", "target": "e1", "element": "Next", "origin": "file:///"}) is None
    assert remember_rule(BROWSER, {"action": "click", "target": "e1", "element": "Next", "origin": "https://example.com/x"}) is None


@pytest.mark.parametrize("args", [{"action": "evil"}, {"action": "snapshot", "extra": 1}, {}, {"action": "navigate"}])
def test_unknown_actions_and_extra_keys_are_blocked(args):
    assert permission_class(BROWSER, args) == "blocked"
    assert _decide(args)[1] is False


def test_other_tools_with_urls_keep_their_old_classification():
    assert classify("WebFetch", {"url": "https://example.com/login"}) == "login"


# --- Bot-side MCP tool -----------------------------------------------------------

SNAPSHOT = """### Page state
- Page URL: https://shop.example.com/cart?session=abc
- Page Title: Cart
- Page Snapshot:
```yaml
- heading "Cart" [level=1] [ref=e3]
- button "Pay now" [ref=e12] [cursor=pointer]
- textbox "Password" [ref=e8]
- generic [ref=e20]: Delete account
- link "Home" [ref=e5] [cursor=pointer]:
  - /url: /home
```
"""


@pytest.fixture
def page(monkeypatch):
    monkeypatch.setattr(srv, "_page_origin", None)
    monkeypatch.setattr(srv, "_page_labels", {})
    monkeypatch.setattr(srv, "_page_elements", {})


def test_mcp_remembers_origin_and_labels_from_page_state(page):
    srv._remember_page(SNAPSHOT)
    assert srv._page_origin == "https://shop.example.com"
    assert srv._page_labels["e12"] == "button Pay now"
    assert srv._page_labels["e8"] == "textbox Password"
    assert srv._page_labels["e20"] == "generic Delete account"
    assert srv._page_labels["e5"] == "link Home"
    args = srv._browser_approval_args({"action": "click", "target": "e12", "element": "Next", "value": SECRET})
    assert args["origin"] == "https://shop.example.com" and args["page_label"] == "button Pay now"
    assert args["value"] == MASK
    assert classify(BROWSER, args) == "pay"


def test_mcp_page_origin_resets_labels_and_ignores_forbidden_origin(page):
    srv._remember_page(SNAPSHOT)
    srv._remember_page("- Page URL: http://127.0.0.1:9222/\n- Page Snapshot:\n- button \"Go\" [ref=e1]")
    assert srv._page_origin is None and "e12" not in srv._page_labels
    assert "origin" not in srv._browser_approval_args({"action": "click", "target": "e1", "element": "Go"})


def _patch_core(monkeypatch, handler):
    monkeypatch.setenv("BOTHUB_URL", "http://core.test")
    monkeypatch.setenv("BOTHUB_TOKEN", "bot:scout:deadbeef")
    monkeypatch.setenv("BOTHUB_THREAD_ID", "thread-1")
    monkeypatch.setenv("BOTHUB_TURN_ID", "turn-1")
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(srv, "_client", lambda: httpx.AsyncClient(base_url="http://core.test", transport=transport))


async def test_approve_sends_no_typed_text_to_the_core(monkeypatch, page):
    seen = []

    def handler(request):
        seen.append(request.content.decode())
        return httpx.Response(200, json={"id": "a1", "status": "approved"})

    _patch_core(monkeypatch, handler)
    original = {"action": "fill", "target": "e8", "element": "Search", "value": SECRET, "url": ""}
    result = await srv.approve(BROWSER, original)
    assert result == {"behavior": "allow", "updatedInput": original}, "the tool itself still gets what the model typed"
    assert SECRET not in seen[0] and MASK in seen[0]
    seen.clear()
    await srv.approve(BROWSER, {"action": "navigate", "url": "https://a.com/p?token=" + SECRET})
    assert SECRET not in seen[0] and "https://a.com/p" in seen[0]


@pytest.mark.parametrize("url", ATTACKS[:12])
async def test_browser_tool_refuses_forbidden_address_before_any_call(monkeypatch, url):
    def handler(request):
        raise AssertionError("the core must not be called")

    _patch_core(monkeypatch, handler)
    result = await srv.browser(action="navigate", url=url)
    assert len(result) == 1 and result[0].startswith("url_forbidden: ")


def test_mcp_remembers_role_and_name_of_each_ref(page):
    srv._remember_page(SNAPSHOT)
    assert srv._page_elements["e12"] == ("button", "Pay now")
    assert srv._page_elements["e8"] == ("textbox", "Password")
    assert srv._page_elements["e20"] == ("generic", "")  # без кавычек имя пустое: текст после двоеточия это не имя
    assert srv._page_labels["e20"] == "generic Delete account", "подпись для риска approvals остаётся"
    assert srv._page_elements["e5"] == ("link", "Home")


def test_the_name_in_quotes_keeps_escaped_quotes_and_backslashes(page):
    srv._remember_page('- Page URL: https://a.example/\n'
                       '- button "Say \\"hi\\" now" [ref=e1]\n'
                       '- link "C:\\\\dir" [ref=e2]\n'
                       '- button "ends with a quote \\"" [ref=e3]\n'
                       '- button "tab\\there" [ref=e4]\n'
                       '- heading "Title" [level=1] [ref=e5]')
    assert srv._page_elements["e1"] == ("button", 'Say "hi" now')
    assert srv._page_elements["e2"] == ("link", "C:\\dir")
    assert srv._page_elements["e3"] == ("button", 'ends with a quote "')
    assert srv._page_elements["e4"] == ("button", "tab\there")
    assert srv._page_elements["e5"] == ("heading", "Title")
    assert srv._page_labels["e1"] == 'button Say "hi" now'


def test_a_name_without_quotes_is_empty_even_with_text_after_the_colon(page):
    srv._remember_page('- Page URL: https://a.example/\n- link [ref=e1]: Read more\n- generic [ref=e2]: Pay now\n- button [ref=e3]')
    assert srv._page_elements["e1"] == ("link", "") and srv._page_elements["e2"] == ("generic", "")
    assert srv._page_elements["e3"] == ("button", "")


def test_the_page_cannot_inject_a_name_through_an_unclosed_quote(page):
    srv._remember_page('- Page URL: https://a.example/\n- button "never closed [ref=e1]\n- button "ok" [ref=e2]')
    assert srv._page_elements.get("e1", ("button", ""))[1] == ""
    assert srv._page_elements["e2"] == ("button", "ok")


def test_the_value_of_an_input_is_never_taken_for_its_name(page):
    srv._remember_page('- Page URL: https://a.example/\n- textbox [ref=e9]: ' + SECRET + '\n- textbox "Mail" [ref=e10]: x')
    assert srv._page_elements["e9"] == ("textbox", "")
    assert srv._page_elements["e10"] == ("textbox", "Mail")


def test_the_elements_are_dropped_when_the_origin_changes(page):
    srv._remember_page(SNAPSHOT)
    srv._remember_page("- Page URL: https://other.example/\n- button \"Go\" [ref=e1]")
    assert "e12" not in srv._page_elements and srv._page_elements["e1"] == ("button", "Go")


async def _browser_with_core(monkeypatch, **call):
    seen = []

    def handler(request):
        seen.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"ok": True, "authorization_id": None})

    async def fake_playwright(name, args):
        return types.SimpleNamespace(isError=False, content=[])

    _patch_core(monkeypatch, handler)
    monkeypatch.setattr(srv, "_browser_call", fake_playwright)
    await srv.browser(**call)
    return {path: body for path, body in seen}


async def test_the_step_sent_to_the_core_carries_role_and_name_of_the_element(monkeypatch, page):
    srv._remember_page(SNAPSHOT)
    sent = await _browser_with_core(monkeypatch, action="click", target="e12", element="Next")
    step = sent["/api/browser/step"]
    assert step["role"] == "button" and step["name"] == "Pay now" and step["target"] == "e12"
    sent = await _browser_with_core(monkeypatch, action="fill", target="e8", value=SECRET, element="x")
    assert sent["/api/browser/step"]["role"] == "textbox" and sent["/api/browser/step"]["name"] == "Password"
    assert SECRET not in json.dumps(sent)


async def test_unknown_ref_and_other_actions_send_no_role_and_name(monkeypatch, page):
    srv._remember_page(SNAPSHOT)
    sent = await _browser_with_core(monkeypatch, action="click", target="e999", element="x")
    assert "role" not in sent["/api/browser/step"] and "name" not in sent["/api/browser/step"]
    sent = await _browser_with_core(monkeypatch, action="navigate", url="https://a.example/")
    assert "role" not in sent["/api/browser/step"]
    assert sent["/api/browser/authorize"].keys() <= {"thread_id", "turn_id", "action", "url"}
