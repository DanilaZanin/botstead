"""Правки по ревью Opus ядра процедур (docs/contracts.md, раздел 14): regex, риск с параметрами, платёжные поля как секрет,
press и navigate, невидимые символы, формат ошибок, match_url, resolve_step.

Чистая логика `bothub.procedures`, без базы и HTTP."""
import asyncio
import concurrent.futures.process
import re
import time

import pytest

from bothub import procedures as P

pytestmark = pytest.mark.pure

SENTINEL = "client-value-4f9a1c"


def step(**fields):
    base = {"id": "s1", "action": "click", "target": {"role": "button", "name": "Next"}}
    return base | fields


CONTEXT = {"url_matches": "^https://example\\.com/"}  # куда привели процедуру: секрет без этого не сохраняется (secret_no_context)


def fill(**fields):
    base = {"id": "s1", "action": "fill", "target": {"role": "textbox", "name": "Email"}}
    if "secret_ref" not in fields and "needs_value" not in fields:
        base["value"] = "x"
    if fields.get("secret_ref") or "{{" in str(fields.get("value", "")):
        base["precondition"] = CONTEXT
    return base | fields


def norm(steps, params=None, **kwargs):
    return P.normalize_procedure(params or [], steps, **kwargs)[1]


def bad(steps, params=None, match="", **kwargs):
    with pytest.raises(P.ProcedureError) as caught:
        P.normalize_procedure(params or [], steps, **kwargs)
    assert match in str(caught.value), str(caught.value)
    return caught.value


def numbered(steps):
    return [dict(item, id=f"s{n}") for n, item in enumerate(steps, 1)]


# --- 1. regex: статическое правило -----------------------------------------------------------------------------

@pytest.mark.parametrize("pattern", [
    "^https://example\\.com/login", "^https://(www\\.)?example\\.com/", "/cart/(en|ru)/", "https://[^/]+/checkout",
    "a{1,3}b", "(?i)^HTTPS://", "^https://example\\.com/(?:a|b)/\\d+$",
    "a{0,11}",  # один неограниченный повтор (11 - 0 > 10) допустим
    "a{0,10}b{0,10}c{0,10}",  # ограниченные: сумма верхних границ 30
    "(a+)?",  # необязательная группа не повторяется больше одного раза
    "a{10}" * 20,  # сумма верхних границ ровно 200
    "^https://example\\.com/.*$",
])
def test_safe_regexes_pass(pattern):
    assert norm([step(expect={"url_matches": pattern}, precondition={"url_matches": pattern})])


@pytest.mark.parametrize("pattern,code", [
    ("(a+", "regex_invalid"),
    ("[a-", "regex_invalid"),
    ("x" * 301, "regex_too_long"),
    # повтор с high - low > 10 считается неограниченным: больше одного
    ("a{0,11}b{0,11}", "regex_unbounded"),
    ("a{5,}b+", "regex_unbounded"),
    (".*a.*b", "regex_unbounded"),
    (".*.*", "regex_unbounded"),
    ("a+b+c+", "regex_unbounded"),
    (".{0,200}x.*", "regex_unbounded"),
    # сумма верхних границ ограниченных повторов
    ("a{10}" * 21, "regex_budget"),
    ("(?:a{10}b{10}){1}" * 11, "regex_budget"),
    # вложенные повторы и повтор внутри группы с повтором
    ("(a+)+$", "regex_nested"),
    ("(a*)*", "regex_nested"),
    ("(.*a){5}", "regex_nested"),
    ("(\\w+\\s?)+", "regex_nested"),
    ("(?:/[^/]+)*/end", "regex_nested"),
    ("(a{2}){3}", "regex_nested"),
    ("(?:ab+){2,5}", "regex_nested"),
    # альтернативы внутри повтора
    ("(a|aa)+$", "regex_alternation"),
    ("(ab|cd)*", "regex_alternation"),
    ("(?:ab|cd){2,5}", "regex_alternation"),
    # обратные ссылки и условные группы
    ("(a)\\1", "regex_backreference"),
    ("(?P<x>a)(?P=x)", "regex_backreference"),
    ("(a)?(?(1)b|c)", "regex_backreference"),
    # lookaround
    ("(?=a)", "regex_lookaround"),
    ("(?!a)", "regex_lookaround"),
    ("(?<=a)b", "regex_lookaround"),
    ("(?<!a)b", "regex_lookaround"),
    ("", "invalid"),
    (5, "invalid"),
])
def test_dangerous_or_broken_regexes_are_rejected_at_save(pattern, code):
    for place in ("precondition", "expect"):
        error = bad([step(**{place: {"url_matches": pattern}})], match=code)
        assert error.code == code or code == "invalid"
        assert error.path == f"steps[0].{place}.url_matches"


def test_regex_errors_carry_the_path_and_never_echo_the_pattern():
    error = bad([step(expect={"url_matches": "(" + SENTINEL})], match="regex_invalid")
    assert SENTINEL not in str(error)


# --- 1б. match_url: отдельный процесс, предел 200 мс -----------------------------------------------------------

@pytest.fixture
def process_pool(monkeypatch):
    # Песочница без sysctl: проверка лимитов (os.sysconf SC_SEM_NSEMS_MAX) даёт EPERM, хотя запуск процессов работает.
    monkeypatch.setattr(concurrent.futures.process, "_check_system_limits", lambda: None)
    monkeypatch.setattr(P, "_pool", None)
    try:
        P._new_pool().shutdown()
    except OSError:
        pytest.skip("процессы с семафорами недоступны в этой среде")
    yield
    P.shutdown_match_pool()


async def test_match_url_without_a_worker_process_fails_closed(monkeypatch):
    def broken():
        raise PermissionError("no semaphores")

    monkeypatch.setattr(P, "_new_pool", broken)
    monkeypatch.setattr(P, "_pool", None)
    assert await P.match_url("a", "a") == (False, True), "не удалось запустить процесс: не совпало, признак таймаута"


@pytest.mark.usefixtures("process_pool")
async def test_match_url_matches_and_does_not_match():
    assert await P.match_url("^https://example\\.com/login", "https://example.com/login?x=1") == (True, False)
    assert await P.match_url("^https://example\\.com/login", "https://other.example/login") == (False, False)
    assert await P.match_url("(", "https://example.com/") == (False, False)  # битый шаблон: не совпало, не таймаут


@pytest.mark.usefixtures("process_pool")
async def test_match_url_cuts_the_address_to_2048_characters():
    tail = "/" + "a" * 2100 + "needle"
    assert await P.match_url("needle", "https://example.com" + tail) == (False, False)
    assert await P.match_url("^https://example\\.com/a{1,3}", "https://example.com/aaa" + "b" * 5000) == (True, False)
    assert P.MATCH_URL_MAX == 2048


@pytest.mark.usefixtures("process_pool")
async def test_a_heavy_pattern_stays_within_the_limit_and_does_not_block_the_event_loop():
    heavy, url = "(a+)+$", "a" * 40 + "!"  # такой шаблон при сохранении отклоняется; здесь он нужен как заведомо тяжёлый
    lags, stop = [], False

    async def ticker():
        last = time.perf_counter()
        while not stop:
            await asyncio.sleep(0.01)
            now = time.perf_counter()
            lags.append(now - last - 0.01)
            last = now

    await P.match_url("a", "a")  # прогрев: процесс уже запущен, предел 200 мс относится к самому сопоставлению
    task = asyncio.create_task(ticker())
    started = time.perf_counter()
    result = await P.match_url(heavy, url)
    elapsed = time.perf_counter() - started
    stop = True
    await task
    assert result == (False, True), "не совпало, признак таймаута"
    assert elapsed < P.MATCH_URL_TIMEOUT + 1.0, elapsed
    assert max(lags) < 0.15, f"event loop простоял {max(lags):.3f} с"
    # после убитого воркера следующий вызов работает
    assert await P.match_url("^https://", "https://example.com/") == (True, False)


@pytest.mark.usefixtures("process_pool")
async def test_parallel_calls_share_one_worker_and_a_timeout_does_not_poison_the_next_one():
    results = await asyncio.gather(P.match_url("(a+)+$", "a" * 40 + "!"), P.match_url("^ok$", "ok"), P.match_url("^no$", "ok"))
    assert results[0] == (False, True)
    assert results[1] == (True, False) and results[2] == (False, False)


# --- 2. риск шага с параметром в цели --------------------------------------------------------------------------

def test_a_param_in_a_selector_is_rejected():
    for place in ("target", "precondition", "expect"):
        selector = {"selector": "#{{item}}"}
        steps = [step(target=selector)] if place == "target" else [step(**{place: {"visible": selector}})]
        error = bad(steps, [{"name": "item"}], match="param_in_selector")
        assert error.path.endswith("selector"), error.path


def test_a_param_in_the_name_or_url_gets_risk_other_at_least():
    params = [{"name": "label"}, {"name": "host"}]
    assert norm([step(target={"role": "button", "name": "{{label}}"})], params)[0]["risk"] == "other"
    assert norm([step(target={"role": "button", "name": "Open {{label}}"})], params)[0]["risk"] == "other"
    assert norm([{"id": "s1", "action": "navigate", "target": {"url": "https://{{host}}/"}}], params)[0]["risk"] == "other"
    assert norm([fill(target={"role": "textbox", "name": "{{label}}"})], params)[0]["risk"] == "other"
    # ровно так же, когда классификатор по тексту не нашёл бы ничего, а без параметра шаг был бы none
    assert norm([step()])[0]["risk"] == "none"
    # более строгая метка не понижается
    assert norm([step(target={"role": "button", "name": "Pay {{label}}"})], params)[0]["risk"] == "pay"


def test_resolve_step_substituting_a_pay_word_gives_pay():
    saved = norm([step(target={"role": "button", "name": "{{label}}"})], [{"name": "label"}])[0]
    assert saved["risk"] == "other"
    resolved, _ = P.resolve_step(saved, {"label": "Оплатить"})
    assert resolved["target"] == {"role": "button", "name": "Оплатить"} and resolved["risk"] == "pay"
    resolved, _ = P.resolve_step(saved, {"label": "Next"})
    assert resolved["risk"] == "other", "риск не ниже сохранённого"


def test_resolve_step_keeps_the_saved_risk_when_the_recomputed_one_is_lower():
    saved = norm([step(risk="delete")])[0]
    assert P.resolve_step(saved, {})[0]["risk"] == "delete"


def test_resolve_step_checks_the_final_address_with_url_forbidden():
    saved = norm([{"id": "s1", "action": "navigate", "target": {"url": "http://{{host}}/latest"}}], [{"name": "host"}])[0]
    with pytest.raises(P.ProcedureError) as caught:
        P.resolve_step(saved, {"host": "169.254.169.254"})
    assert "url_forbidden" in str(caught.value) and "169.254.169.254" not in str(caught.value)
    resolved, _ = P.resolve_step(saved, {"host": "example.com"})
    assert resolved["target"]["url"] == "http://example.com/latest"
    with pytest.raises(P.ProcedureError):
        P.resolve_step(saved, {"host": "example.com@127.0.0.1"})


def test_resolve_step_recomputes_the_navigation_risk_from_the_final_address():
    saved = norm([{"id": "s1", "action": "navigate", "target": {"url": "https://{{host}}/"}}], [{"name": "host"}])[0]
    assert P.resolve_step(saved, {"host": "shop.example.com"})[0]["risk"] == "other"
    assert P.resolve_step(saved, {"host": "pay.example.com"})[0]["risk"] == "pay"


def test_a_substituted_value_is_not_expanded_again():
    params = [{"name": "first"}, {"name": "other"}]
    saved = norm([fill(value="{{first}}")], params)[0]
    resolved, _ = P.resolve_step(saved, {"first": "{{other}}", "other": "SECOND"})
    assert resolved["value"] == "{{other}}"
    saved = norm([step(target={"role": "button", "name": "{{first}}"})], params)[0]
    assert P.resolve_step(saved, {"first": "{{other}}", "other": "Оплатить"})[0]["target"]["name"] == "{{other}}"


def test_resolve_step_formats_numbers_and_booleans_and_needs_every_param():
    saved = norm([fill(value="{{n}}/{{flag}}")], [{"name": "n", "type": "number"}, {"name": "flag", "type": "boolean"}])[0]
    assert P.resolve_step(saved, {"n": 3, "flag": True})[0]["value"] == "3/true"
    assert P.resolve_step(saved, {"n": 2.5, "flag": False})[0]["value"] == "2.5/false"
    with pytest.raises(P.ProcedureError) as caught:
        P.resolve_step(saved, {"n": 3})
    assert "param_missing" in str(caught.value)


def test_resolve_step_returns_the_values_that_must_not_be_logged():
    saved = norm([fill(target={"role": "textbox", "name": "Password"}, secret_ref="vault:bank")])[0]
    resolved, hidden = P.resolve_step(saved, {}, lambda name: SENTINEL if name == "bank" else None)
    assert resolved["value"] == SENTINEL and hidden == {SENTINEL}
    with pytest.raises(P.ProcedureError) as caught:
        P.resolve_step(saved, {}, lambda name: None)
    assert "secret_not_found" in str(caught.value) and "bank" not in str(caught.value)
    with pytest.raises(P.ProcedureError):
        P.resolve_step(saved, {})  # без поиска секретов значение взять неоткуда
    saved = norm([fill(target={"role": "textbox", "name": "Password"}, value="{{pw}}")], [{"name": "pw", "secret": True}])[0]
    resolved, hidden = P.resolve_step(saved, {"pw": "vault:bank"}, lambda name: SENTINEL, secret_params={"pw"})
    assert resolved["value"] == SENTINEL and hidden == {SENTINEL}
    # обычное значение в обычное поле не скрывается
    plain = norm([fill(value="{{e}}")], [{"name": "e"}])[0]
    assert P.resolve_step(plain, {"e": "a@b.c"}) == ({**plain, "value": "a@b.c"}, set())


def test_a_secret_field_reached_through_a_param_in_the_name_needs_a_secret_value():
    saved = norm([fill(target={"role": "textbox", "name": "{{label}}"}, value="plain")], [{"name": "label"}])[0]
    with pytest.raises(P.ProcedureError) as caught:
        P.resolve_step(saved, {"label": "CVV"})
    assert "secret_required" in str(caught.value)
    assert P.resolve_step(saved, {"label": "Email"})[0]["value"] == "plain"


# --- 3. платёжные поля и коды как секрет -----------------------------------------------------------------------

SECRET_FIELDS = ["CVV", "CVC", "Card number", "Номер карты", "Срок действия", "Security code", "Код подтверждения",
                 "Одноразовый код", "OTP", "2FA", "Passwort", "Kennwort", "Contraseña", "Mot de passe", "Senha", "Пароль"]


@pytest.mark.parametrize("name", SECRET_FIELDS)
def test_payment_and_code_fields_require_secret_ref(name):
    target = {"role": "textbox", "name": name}
    error = bad([fill(target=target, value=SENTINEL)], match="secret_required")
    assert SENTINEL not in str(error) and error.path == "steps[0].value"
    assert norm([fill(target=target, secret_ref="vault:card")])[0]["secret_ref"] == "vault:card"
    assert norm([fill(target=target, value="{{p}}")], [{"name": "p", "secret": True}])
    assert P.secret_field("textbox", name) is True


@pytest.mark.parametrize("name", [
    "C V V", "ＣＶＶ", "Card Number", "CARD NUMBER", "Номер  карты", "MOT DE PASSE", "mot\tde\npasse", "Pass\u202eword",
    "Pa\u200bssword", "Contraseña", "Ihr Passwort bestätigen", "Enter your 2 FA code", "срок действия карты",
])
def test_the_field_name_is_normalized_before_matching(name):
    assert P.secret_field("textbox", name) is True, name


@pytest.mark.parametrize("name", ["Email", "Имя", "Search", "Телефон", "Comment"])
def test_ordinary_fields_are_not_secret(name):
    assert P.secret_field("textbox", name) is False
    assert norm([fill(target={"role": "textbox", "name": name}, value="x")])


def test_events_mark_every_secret_field_with_needs_secret():
    def event(name):
        return {"action": "fill", "result": "ok", "role": "textbox", "name": name, "value": "[redacted]"}

    for name in SECRET_FIELDS:
        steps = P.events_to_steps([event(name)])
        assert steps[0]["needs_value"] is True and steps[0]["needs_secret"] is True, name
    assert "needs_secret" not in P.events_to_steps([event("Email")])[0]


def test_an_unnamed_field_and_a_selector_field_need_value_and_secret_from_events():
    unnamed = P.events_to_steps([{"action": "fill", "result": "ok", "role": "textbox", "name": "", "value": "[redacted]"}])[0]
    assert unnamed["needs_value"] is True and unnamed["needs_secret"] is True
    blank = P.events_to_steps([{"action": "fill", "result": "ok", "role": "textbox", "name": "  \u200b ", "value": "[redacted]"}])[0]
    assert blank["needs_secret"] is True
    # проходит нормализацию как черновик
    assert P.normalize_procedure([], [unnamed])[1][0]["needs_secret"] is True


def test_manual_save_may_use_a_literal_in_an_unnamed_or_selector_field_but_not_below_other():
    for target in ({"role": "textbox", "name": ""}, {"selector": "input.q"}):
        out = norm([fill(target=target, value="literal")])[0]
        assert out["value"] == "literal" and out["risk"] == "other"
    bad([fill(target={"selector": "input.q"}, value="literal", risk="none")], match="risk_below_computed")
    assert norm([fill(target={"role": "textbox", "name": "Email"}, value="x")])[0]["risk"] == "none"
    # needs_value в таком поле: подсказка о секрете сохраняется
    out = norm([fill(target={"selector": "input.q"}, value=None, needs_value=True, needs_secret=True)])[0]
    assert out["needs_secret"] is True and out["risk"] == "other"


# --- 4. press, navigate ----------------------------------------------------------------------------------------

PASSWORD = {"role": "textbox", "name": "Password"}
CARD = {"role": "textbox", "name": "Card number"}
EMAIL = {"role": "textbox", "name": "Email"}


def press(value="Enter", **fields):
    return {"id": "s1", "action": "press", "value": value} | fields


@pytest.mark.parametrize("key", ["Enter", "NumpadEnter", "Space", "enter", " "])
def test_press_submit_key_inherits_the_form_risk(key):
    steps = norm(numbered([fill(target=PASSWORD, secret_ref="vault:a"), press(key)]))
    assert steps[1]["risk"] == "login"
    steps = norm(numbered([fill(target=EMAIL), fill(target=CARD, secret_ref="vault:card"), press(key)]))
    assert steps[2]["risk"] == "pay"
    steps = norm(numbered([step(target={"role": "combobox", "name": "Card number"}, action="select", value="x"), press(key)]))
    assert steps[1]["risk"] == "pay"


def test_press_after_harmless_fields_stays_none_and_other_keys_do_not_inherit():
    assert norm(numbered([fill(target=EMAIL), press("Enter")]))[1]["risk"] == "none"
    assert norm(numbered([fill(target=PASSWORD, secret_ref="vault:a"), press("Tab")]))[1]["risk"] == "none"
    assert norm(numbered([press("Enter")]))[0]["risk"] == "none"


def test_the_form_ends_at_navigate_and_a_click_without_risk_does_not_end_it():
    # Пункт 10 ревью Opus: «Далее» без риска между вводом карты и Enter форму не закрывает.
    after_click = norm(numbered([fill(target=PASSWORD, secret_ref="vault:a"), step(), press("Enter")]))
    assert [s["risk"] for s in after_click] == ["login", "none", "login"]
    card_form = norm(numbered([fill(target=CARD, secret_ref="vault:card"), step(), press("Enter")]))
    assert [s["risk"] for s in card_form] == ["pay", "none", "pay"] and card_form[2]["flags"] == ["login"]
    wait = {"id": "s1", "action": "wait", "value": "100"}
    assert norm(numbered([fill(target=CARD, secret_ref="vault:card"), wait, press("Tab"), press("Enter")]))[3]["risk"] == "pay"
    navigate = {"id": "s1", "action": "navigate", "target": {"url": "https://example.com/"}}
    after_navigate = norm(numbered([fill(target=PASSWORD, secret_ref="vault:a"), navigate, press("Enter")]))
    assert after_navigate[2]["risk"] == "none"
    two_forms = norm(numbered([fill(target=PASSWORD, secret_ref="vault:a"), press("Enter"), navigate, fill(target=EMAIL), press("Enter")]))
    assert [s["risk"] for s in two_forms] == ["login", "login", "none", "none", "none"]


def test_press_with_a_target_is_a_click_and_keeps_the_form_risk():
    steps = norm(numbered([fill(target=PASSWORD, secret_ref="vault:a"),
                           press("Enter", target={"role": "button", "name": "Next"}), press("Enter")]))
    assert steps[1]["risk"] == "none" and steps[2]["risk"] == "login"
    assert norm([press("Enter", target={"role": "button", "name": "Pay"})])[0]["risk"] == "pay"


def test_a_step_that_needs_login_and_pay_keeps_the_stricter_and_flags_the_other():
    steps = norm(numbered([fill(target=PASSWORD, secret_ref="vault:a"), fill(target=CARD, secret_ref="vault:card"), press("Enter")]))
    assert steps[2]["risk"] == "pay" and steps[2]["flags"] == ["login"]
    assert "flags" not in steps[0] and steps[0]["risk"] == "login"
    assert steps[1]["risk"] == "pay" and steps[1]["flags"] == ["login"], "поле карты и деньги, и секрет"
    # одна метка: флагов нет
    assert "flags" not in norm(numbered([fill(target=PASSWORD, secret_ref="vault:a"), press("Enter")]))[1]
    # кнопка, подходящая под обе метки сразу
    both = norm([step(target={"role": "button", "name": "Sign in and pay"})])[0]
    assert both["risk"] == "pay" and both["flags"] == ["login"]


def test_the_client_cannot_lower_the_inherited_risk_and_flags_are_not_taken_from_the_client():
    bad(numbered([fill(target=PASSWORD, secret_ref="vault:a"), press("Enter", risk="none")]), match="risk_below_computed")
    out = norm(numbered([fill(target=EMAIL), press("Enter", flags=["pay"])]))
    assert "flags" not in out[1]


@pytest.mark.parametrize("url,risk", [
    ("https://shop.example.com/checkout", "pay"), ("https://pay.example.com/", "pay"),
    ("https://example.com/billing/plan", "pay"), ("https://example.com/oplata", "pay"),
    ("https://example.com/payment?x=1", "pay"), ("https://example.com/pay-now", "pay"),
    ("https://example.com/a_pay_b", "pay"), ("https://checkout-example.com/", "pay"),
    ("https://example.com/?next=/pay", "pay"), ("https://billing.example.com/", "pay"),
    ("https://example.com/paypal", "none"), ("https://example.com/prepaid", "none"),
    ("https://example.com/payload", "none"), ("https://example.com/", "none"), ("about:blank", "none"),
])
def test_navigate_to_a_payment_address_gets_pay(url, risk):
    assert norm([{"id": "s1", "action": "navigate", "target": {"url": url}}])[0]["risk"] == risk


def test_press_of_a_single_printable_character_is_not_logged():
    saved = norm([press("a")])[0]
    assert P.resolve_step(saved, {})[1] == {"a"}
    assert P.resolve_step(norm([press("Enter")])[0], {})[1] == set()
    assert P.resolve_step(norm([press("1")])[0], {})[1] == {"1"}
    param = norm([press("{{k}}")], [{"name": "k"}])[0]
    assert P.resolve_step(param, {"k": "x"})[1] == {"x"}
    assert P.resolve_step(param, {"k": "Escape"})[1] == set()


# --- 5. управляющие и невидимые символы ------------------------------------------------------------------------

@pytest.mark.parametrize("char", ["\u202e", "\u200b", "\ufeff", "\u2066", "\x07", "\n", "\t", "\x00", "\u0085", "\u00ad"])
def test_a_name_with_control_or_format_characters_is_rejected_for_procedures(char):
    target = {"role": "button", "name": f"Pay{char}ment"}
    for steps in ([step(target=target)], [step(precondition={"visible": target})], [step(expect={"visible": target})]):
        error = bad(steps, match="control_chars")
        assert error.path.endswith(".name")


def test_a_role_with_a_hidden_character_is_rejected_and_plain_spaces_and_nbsp_are_fine():
    bad([step(target={"role": "but\u202eton", "name": "Next"})], match="steps[0].target.role")
    assert norm([step(target={"role": "button", "name": "Next page"})])
    assert norm([step(target={"role": "button", "name": "Next page"})])


def test_strip_invisible_removes_cc_and_cf_but_keeps_spaces():
    assert P.strip_invisible("Pay\u202e now\x07\n\u200b!") == "Pay now!"
    assert P.strip_invisible("a b c") == "a b c"
    assert P.strip_invisible("") == ""


def test_secret_field_ignores_hidden_characters_but_need_not_recognize_a_stripped_name():
    assert P.secret_field("textbox", "Pass\u202eword") and P.secret_field("textbox", "P\u200ba\u200bs\u200bs\u200bw\u200bo\u200br\u200bd")
    assert P.secret_field("textbox", "\u202e\u200b") is False
    assert P.secret_field(None, None) is False


# --- 6. url_matches: null ---------------------------------------------------------------------------------------

def test_url_matches_null_is_the_same_as_absent():
    target = {"role": "button", "name": "Go"}
    out = norm([step(precondition={"url_matches": None, "visible": target}, expect={"url_matches": None, "visible": target})])[0]
    assert out["precondition"] == {"visible": target} and out["expect"] == {"visible": target}
    out = norm([step(precondition={"url_matches": None}, expect={"url_matches": None, "text": None})])[0]
    assert out["precondition"] is None and out["expect"] is None


# --- 8. понижение риска ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("client", ["none", "other", "send"])
def test_lowering_the_risk_below_computed_is_an_error_with_the_path(client):
    error = bad(numbered([step(), step(target={"role": "button", "name": "Pay now"}, risk=client)]), match="risk_below_computed")
    assert error.path == "steps[1].risk" and str(error).startswith("steps[1].risk: risk_below_computed: ")


def test_equal_or_higher_risk_is_kept_and_a_missing_risk_is_computed():
    assert norm([step(target={"role": "button", "name": "Pay now"}, risk="pay")])[0]["risk"] == "pay"
    assert norm([step(target={"role": "button", "name": "Pay now"})])[0]["risk"] == "pay"
    assert norm([step(risk="delete")])[0]["risk"] == "delete"


@pytest.mark.parametrize("client", ["none", "other", "send", None])
def test_import_and_from_turn_just_recompute_the_risk(client):
    fields = {} if client is None else {"risk": client}
    out = norm([step(target={"role": "button", "name": "Pay now"}, **fields)], strict_risk=False)[0]
    assert out["risk"] == "pay"
    assert norm([step(risk="delete")], strict_risk=False)[0]["risk"] == "delete"


def test_unknown_risk_has_a_path_and_a_code():
    error = bad(numbered([step(), step(risk="boom")]), match="risk_invalid")
    assert error.path == "steps[1].risk"


# --- 7. computed_risk ------------------------------------------------------------------------------------------

def test_computed_risk_is_the_floor_without_the_clients_raise():
    steps = norm(numbered([step(risk="delete"), step(target={"role": "button", "name": "Pay now"}), fill(target=PASSWORD, secret_ref="vault:a"),
                           press("Enter")]))
    annotated = P.with_computed_risk(steps)
    assert [s["risk"] for s in annotated] == ["delete", "pay", "login", "pay"]  # Enter наследует и оплату предыдущего клика
    assert [s["computed_risk"] for s in annotated] == ["none", "pay", "login", "pay"]
    assert all("computed_risk" not in s for s in steps), "исходные шаги не меняются"


def test_computed_risk_from_the_client_is_ignored_and_not_stored():
    out = norm([step(computed_risk="pay")])[0]
    assert "computed_risk" not in out and out["risk"] == "none"


# --- 9. формат ошибок: путь, код, короткий текст ---------------------------------------------------------------

ERROR_FORMAT = re.compile(r"^[a-z_]+(?:\[\d+\])?(?:\.[a-z_]+(?:\[\d+\])?)*: [a-z_]+: [^:\n][^\n]{0,120}$")

BROKEN_STEPS = [
    [{"id": "s1", "action": "nope"}], [{"id": "bad id", "action": "click"}], ["x"], [step(target={"role": "x"})],
    [step(extra=1)], [fill(value=None)], [fill(value="x", secret_ref="vault:a")], [fill(secret_ref="nope")],
    [step(value="x")], [step(target={"role": "button", "name": "{{x}}"})], [fill(value="{{")],
    [{"id": "s1", "action": "navigate", "target": {"url": "http://127.0.0.1/"}}], [step(risk="boom")],
    [step(precondition={"url_matches": "(a+)+"})], [step(expect={"timeout_ms": 5})], [fill(target={"role": "textbox", "name": "Password"})],
    [{"id": "s1", "action": "wait", "value": "abc"}], [{"id": "s1", "action": "wait"}], [{"id": "s1", "action": "press"}],
    [step(target={"selector": "#{{p}}"})], [step(needs_value=True)], [fill(needs_secret=True)],
    [step(), step()],
]


@pytest.mark.parametrize("steps", BROKEN_STEPS)
def test_every_step_error_is_path_code_short_text(steps):
    with pytest.raises(P.ProcedureError) as caught:
        P.normalize_procedure([{"name": "p"}], steps)
    text = str(caught.value)
    assert ERROR_FORMAT.match(text), text
    assert caught.value.path and caught.value.code and text == f"{caught.value.path}: {caught.value.code}: {text.split(': ', 2)[2]}"
    assert caught.value.path.startswith("steps"), text


@pytest.mark.parametrize("params", [
    "x", [{"name": "1bad"}], [{"name": "a"}, {"name": "a"}], [{"name": "a", "type": "object"}], [{"name": "a", "secret": True, "default": "x"}],
    [{"name": "a", "extra": 1}], [{"name": f"p{n}"} for n in range(51)], [{"name": "a", "required": 1}],
])
def test_every_param_error_is_path_code_short_text(params):
    with pytest.raises(P.ProcedureError) as caught:
        P.normalize_procedure(params, [])
    assert ERROR_FORMAT.match(str(caught.value)), str(caught.value)
    assert caught.value.path.startswith("params")


def test_name_description_and_import_errors_have_the_same_shape():
    for call, arg in ((P.check_name, " "), (P.check_name, 5), (P.check_name, "x" * 121), (P.check_description, "x" * 2001),
                      (P.parse_import, {"name": "a", "owner_id": "x"}), (P.parse_import, {"name": "a", "format": "x/1"}),
                      (P.parse_import, [])):
        with pytest.raises(P.ProcedureError) as caught:
            call(arg)
        assert ERROR_FORMAT.match(str(caught.value)), str(caught.value)


def test_specific_codes():
    assert bad([fill(value=None)], match="steps[0].value: required: ").code == "required"
    assert bad([step(), step()], match="steps[1].id: duplicate: ").code == "duplicate"
    assert bad([step(target={"role": "button", "name": "{{x}}"})], match="steps[0].target.name: param_undeclared: ").code == "param_undeclared"
    assert bad([fill(target={"role": "textbox", "name": "Password"}, value="x")], match="steps[0].value: secret_required: ")
    assert bad([{"id": "s1", "action": "navigate", "target": {"url": "http://127.0.0.1/"}}], match="steps[0].target.url: url_forbidden: ")
    assert bad([step(extra=1)], match="steps[0]: unknown_field: ")
    assert bad([step(value="x")], match="steps[0].value: not_allowed: ")


def test_no_error_echoes_the_value_that_was_sent():
    for steps in ([fill(value=SENTINEL, target={"role": "textbox", "name": "CVV"})], [step(extra=SENTINEL)],
                  [step(target={"role": "button", "name": SENTINEL + "\u202e"})], [step(risk=SENTINEL)],
                  [step(precondition={"url_matches": "(a+)+" + SENTINEL})], [{"id": SENTINEL + " ", "action": "click"}],
                  [{"id": "s1", "action": "navigate", "target": {"url": "http://127.0.0.1/" + SENTINEL}}]):
        with pytest.raises(P.ProcedureError) as caught:
            P.normalize_procedure([], steps)
        assert SENTINEL not in str(caught.value), steps
