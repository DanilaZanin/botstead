"""Hardening round 2, pure logic: input values in page state, percent-encoded hosts, pay labels. No Postgres."""
import types

import httpx
import pytest

from bothub import mcp_server as srv
from bothub.browser_control import (
    HIDDEN, BrowserEventMasker, mask_browser_result, mask_browser_text, mask_input_values, url_forbidden, url_origin)
from bothub.risk import classify

pytestmark = pytest.mark.pure
BROWSER = "mcp__bothub__browser"


# --- Input values in page state --------------------------------------------------

def test_plain_value_after_attributes_is_hidden_and_structure_kept():
    text = '- textbox "Password" [active] [ref=e5]: hunter2'
    assert mask_input_values(text) == f'- textbox "Password" [active] [ref=e5]: {HIDDEN}'


def test_unquoted_name_format_from_the_task():
    assert mask_input_values("textbox Password [active] [ref=e5]: hunter2") == \
        f"textbox Password [active] [ref=e5]: {HIDDEN}"


def test_quoted_value_is_hidden():
    assert mask_input_values('- textbox "Password" [ref=e5]: "hunter2: x"') == \
        f'- textbox "Password" [ref=e5]: {HIDDEN}'
    assert mask_input_values('  - searchbox "Search" [ref=e2]: "a \\"quoted\\" word"') == \
        f'  - searchbox "Search" [ref=e2]: {HIDDEN}'


@pytest.mark.parametrize("role", ["textbox", "searchbox", "combobox", "spinbutton", "slider"])
def test_all_input_roles(role):
    assert mask_input_values(f'- {role} "Field" [ref=e1]: secret') == f'- {role} "Field" [ref=e1]: {HIDDEN}'


def test_slider_value_is_hidden_and_selected_option_is_not():
    text = "\n".join([
        '- slider "Volume" [ref=e3]: 42',
        '- combobox "Country" [ref=e4]: Russia',
        '  - option "Russia" [selected] [ref=e5]',
        '  - option "Germany" [ref=e6]',
    ])
    assert mask_input_values(text) == "\n".join([
        f'- slider "Volume" [ref=e3]: {HIDDEN}',
        f'- combobox "Country" [ref=e4]: {HIDDEN}',
        '  - option "Russia" [selected] [ref=e5]',
        '  - option "Germany" [ref=e6]',
    ])
    assert mask_input_values('- option "Russia" [selected]') == '- option "Russia" [selected]'
    assert mask_input_values('- option "Russia" [selected]: Russia') == '- option "Russia" [selected]: Russia'
    assert mask_input_values('- slider "Volume" [ref=e3]') == '- slider "Volume" [ref=e3]'


def test_value_containing_bracket_colon_is_hidden_whole():
    out = mask_input_values('- textbox "Password" [ref=e5]: ab]: cd [ref=e9]: tail')
    assert out == f'- textbox "Password" [ref=e5]: {HIDDEN}'
    assert "ab" not in out and "tail" not in out


def test_name_containing_bracket_colon_does_not_confuse_the_parser():
    out = mask_input_values('- textbox "Pass]: word" [ref=e5]: hunter2')
    assert out == f'- textbox "Pass]: word" [ref=e5]: {HIDDEN}'


def test_multiline_values_are_hidden_whole():
    text = '\n'.join([
        '- textbox "Notes" [ref=e4]: first line',
        '  second secret line',
        '  - looks like a node but is value',
        '- button "Send" [ref=e6]',
    ])
    assert mask_input_values(text) == f'- textbox "Notes" [ref=e4]: {HIDDEN}\n- button "Send" [ref=e6]'
    block = '\n'.join(['- textbox "Bio" [ref=e4]: |', '    line one', '', '    line two', '- button "Go" [ref=e7]'])
    assert mask_input_values(block) == f'- textbox "Bio" [ref=e4]: {HIDDEN}\n- button "Go" [ref=e7]'
    raw_quote = '\n'.join(['- textbox "Bio" [ref=e4]: "one', 'two', 'three" tail', '- button "Go" [ref=e7]'])
    assert mask_input_values(raw_quote) == f'- textbox "Bio" [ref=e4]: {HIDDEN}\n- button "Go" [ref=e7]'
    escaped = '- textbox "Bio" [ref=e4]: "one\\ntwo\\nthree"\n- button "Go" [ref=e7]'
    assert mask_input_values(escaped) == f'- textbox "Bio" [ref=e4]: {HIDDEN}\n- button "Go" [ref=e7]'


def test_unclosed_quote_does_not_swallow_the_page():
    text = '- textbox "P" [ref=e1]: "hunter2\n' + '\n'.join(f'- button "B{n}" [ref=e{n + 10}]' for n in range(60))
    out = mask_input_values(text)
    assert "hunter2" not in out and 'button "B59"' in out


def test_snapshot_fence_and_headers_survive():
    text = '\n'.join([
        '### Page state', '- Page URL: https://example.com/', '- Page Snapshot:', '```yaml',
        '- generic [ref=e1]:', '  - textbox "Email" [ref=e3]: me@example.com',
        '  - textbox "Password" [active] [ref=e5]: hunter2', '  - button "Sign in" [ref=e6] [cursor=pointer]',
        '```'])
    out = mask_input_values(text)
    assert "me@example.com" not in out and "hunter2" not in out
    assert out.count(HIDDEN) == 2
    assert out.endswith('  - button "Sign in" [ref=e6] [cursor=pointer]\n```')
    assert '- textbox "Password" [active] [ref=e5]: ' in out


def test_ordinary_text_is_not_masked():
    text = '\n'.join([
        '- heading "Password: choose a strong one" [level=2] [ref=e2]',
        '- paragraph [ref=e3]: textbox is a word and so is combobox: here',
        '- button "textbox" [ref=e4]',
        '- generic [ref=e20]: Delete account',
        '- link "Home" [ref=e5] [cursor=pointer]:', '  - /url: /home',
        '- textbox "Empty" [ref=e7]',
        '- combobox "Country" [ref=e8]:', '  - option "Russia" [selected]', '  - option "USA"',
        'Playwright said: all good',
    ])
    assert mask_input_values(text) == text
    assert mask_input_values("") == "" and mask_input_values(None) is None


def test_masking_is_idempotent():
    once = mask_input_values('- textbox "Password" [ref=e5]: hunter2\n  extra')
    assert mask_input_values(once) == once


def test_crlf_lines():
    assert mask_input_values('- textbox "P" [ref=e5]: hunter2\r\n- button "Go" [ref=e6]\r\n') == \
        f'- textbox "P" [ref=e5]: {HIDDEN}\r\n- button "Go" [ref=e6]\r\n'


def test_long_adversarial_line_is_fast():
    text = '- textbox ' + '"a" ' * 5000 + '[ref=e1] ' * 2000 + 'x' * 100000
    mask_input_values(text)  # must return, not hang


def test_browser_text_and_event_masker_hide_values():
    snapshot = '- Page URL: https://a.com/p?token=zzz\n- textbox "Password" [ref=e5]: hunter2'
    text = mask_browser_text(snapshot)
    assert "hunter2" not in text and "token=zzz" not in text and HIDDEN in text
    assert "hunter2" not in mask_browser_result("snapshot", snapshot)
    masker = BrowserEventMasker()
    masker.call({"tool": BROWSER, "call_id": "c1", "args": {"action": "snapshot"}})
    result = masker.result({"call_id": "c1", "ok": True, "summary": snapshot,
                            "data": {"result": snapshot, "other": "hunter2"}})
    assert "hunter2" not in result["summary"] and "hunter2" not in result["data"]["result"]


def _patch_core(monkeypatch):
    def handler(request):
        if request.url.path == "/api/browser/authorize":
            return httpx.Response(200, json={"authorization_id": "auth-1"})
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setenv("BOTHUB_URL", "http://core.test")
    monkeypatch.setenv("BOTHUB_TOKEN", "bot:scout:deadbeef")
    monkeypatch.setenv("BOTHUB_THREAD_ID", "thread-1")
    monkeypatch.setenv("BOTHUB_TURN_ID", "turn-1")
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(srv, "_client", lambda: httpx.AsyncClient(base_url="http://core.test", transport=transport))
    monkeypatch.setattr(srv, "_page_origin", None)
    monkeypatch.setattr(srv, "_page_labels", {})


async def test_mcp_browser_tool_hides_values_from_the_model_and_from_approval_labels(monkeypatch):
    _patch_core(monkeypatch)
    page = ('### Page state\n- Page URL: https://shop.example.com/login\n- Page Snapshot:\n```yaml\n'
            '- textbox "Password" [active] [ref=e5]: hunter2\n- button "Sign in" [ref=e6]\n```')

    async def fake_call(name, args):
        return types.SimpleNamespace(isError=False, content=[types.SimpleNamespace(type="text", text=page)])

    monkeypatch.setattr(srv, "_browser_call", fake_call)
    out = await srv.browser(action="snapshot")
    assert len(out) == 1 and "hunter2" not in out[0] and HIDDEN in out[0]
    assert 'textbox "Password" [active] [ref=e5]' in out[0]
    assert "hunter2" not in srv._page_labels["e5"]
    approval = srv._browser_approval_args({"action": "fill", "target": "e5", "element": "Password"})
    assert "hunter2" not in repr(approval)


# --- Percent-encoded hosts ----------------------------------------------------------

@pytest.mark.parametrize("url", [
    "http://%31%32%37.0.0.1/",            # 127.0.0.1
    "http://127%2e0%2e0%2e1/admin",       # dots encoded
    "http://%6c%6f%63%61%6c%68%6f%73%74/",  # localhost
    "http://%6cocalhost:8000/",
    "http://169.254.169.254%2f/latest",    # slash decoded into the host
    "http://example.com%40127.0.0.1/",     # @ decoded into the host
    "http://127.0.0.1%252e/",              # double encoding: % remains after one decoding
    "http://%2531%2532%2537.0.0.1/",
    "http://exam%ple.com/",                # not a valid escape: % stays
    "http://%ff%fe.example.com/",          # not UTF-8
    "http://[fe80::1%25eth0]/",
    "http://%0a127.0.0.1/",
])
def test_percent_encoded_hosts_are_judged_after_decoding_and_leftover_percent_is_refused(url):
    assert url_forbidden(url), url
    assert url_origin(url) is None


@pytest.mark.parametrize("url", [
    "https://example.com/a%20b?q=%41%2F%42#frag%20x",
    "https://example.com/%2e%2e/%31%32%37.0.0.1/",
    "https://example.com/search?host=%31%32%37.0.0.1&u=http%3A%2F%2Flocalhost%2F",
    "https://example.com:8443/p%25q?x=100%25",
    "http://example.org/%7Euser/",
])
def test_percent_in_path_and_query_is_fine(url):
    assert url_forbidden(url) is None, url


def test_encoded_public_host_is_decoded_not_refused():
    assert url_forbidden("https://example%2ecom/") is None
    assert url_forbidden("https://%65xample.com/") is None


# --- Unicode dots and trailing dots ------------------------------------------------------

@pytest.mark.parametrize("url", [
    "http://169.254.169.254\u3002/",
    "http://127.0.0.1\u3002/",
    "http://10.0.0.1\uff61/",
    "http://127.0.0.1\uff0e/",
    "http://%31%32%37.0.0.1%E3%80%82/",
    "http://core\u3002/",
    "http://localhost\u3002/",
    "http://127.0.0.1\u3002:9222/json/version",
    "http://127.0.0.1../",
    "http://127.0.0.1.\u3002/",
    "http://127.0.0.1\u3002\uff61\uff0e./",
    "http://127\u30020\u30020\u30021/",
    "http://127.0.0.1%E3%80%82%E3%80%82/",
    "http://localhost.\u3002.\uff0e/",
    "http://\uff11\uff12\uff17.0.0.1\uff61/",
    "http://127.0.0.1\u2024/",             # one dot leader: NFKC makes it "."
    "http://127.0.0.1\u2025/",             # two dot leader: NFKC makes it ".."
    "http://127.0.0.1\ufe52/",             # small full stop
])
def test_unicode_and_repeated_trailing_dots_do_not_hide_a_forbidden_host(url):
    assert url_forbidden(url), url
    assert url_origin(url) is None


@pytest.mark.parametrize("url", [
    "https://example.com./",
    "https://example.com\u3002/",
    "https://example.com\uff61\u3002/",
    "https://example.com..\u3002/x",
])
def test_trailing_dots_on_a_public_host_stay_allowed(url):
    assert url_forbidden(url) is None, url


# --- pay labels -----------------------------------------------------------------------

def _label(element, action="click"):
    return classify(BROWSER, {"action": action, "target": "e9", "element": element, "origin": "https://example.com"})


@pytest.mark.parametrize("element", ["Картинки", "Яндекс Карты", "Sort by order date", "Payload size",
                                     "Order history", "Карты", "Map view", "Card game"])
def test_false_pay_labels_are_gone(element):
    assert _label(element) != "pay", element


@pytest.mark.parametrize("element", ["Оплатить картой", "Pay now", "Place order", "Оформить заказ", "Checkout",
                                     "Complete your order", "Order now", "Payment", "Pay", "Buy", "Credit card",
                                     "Card number", "Номер карты", "Банковская карта"])
def test_real_pay_labels_stay(element):
    assert _label(element) == "pay", element


@pytest.mark.parametrize("element", [
    "Привязать карту", "Добавить карту", "Сохранить карту", "Оплатить картой", "Подтвердить платёж",
    "Пополнить баланс", "Перевести", "Перевод средств", "Вывести средства", "Продлить подписку",
    "Transfer", "Top up", "Withdraw", "Renew subscription", "Add card", "Save card", "Pay now",
    "Place order", "Checkout", "Оформить заказ", "Подтвердить платеж", "Add a new card"])
def test_card_and_money_actions_are_pay(element):
    assert _label(element) == "pay", element


@pytest.mark.parametrize("element", [
    "Картинки", "Яндекс Карты", "Карта сайта", "Карточка товара", "Payload size", "Sort by order date",
    "Перевод текста", "Переводчик", "Transfer-Encoding", "Renew", "Добавить картинку", "Сохранить карточку"])
def test_non_payment_labels_are_not_pay(element):
    assert _label(element) != "pay", element
