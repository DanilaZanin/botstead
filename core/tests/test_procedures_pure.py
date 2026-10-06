"""Процедуры (раздел 14 контракта): проверка и нормализация шагов и параметров, риск, события browser_step в шаги.

Чистая логика `bothub.procedures`, без базы и HTTP."""
import pytest

from bothub import procedures as P

pytestmark = pytest.mark.pure

SENTINEL = "client-value-4f9a1c"


def step(**fields):
    base = {"id": "s1", "action": "click", "target": {"role": "button", "name": "Next"}}
    return base | fields


def norm(steps, params=None):
    return P.normalize_procedure(params or [], steps)[1]


def bad(steps, params=None, match=""):
    with pytest.raises(P.ProcedureError) as caught:
        P.normalize_procedure(params or [], steps)
    assert match in str(caught.value), str(caught.value)
    return str(caught.value)


# --- параметры -------------------------------------------------------------------------------------------------

def test_param_defaults_are_filled_in():
    params, _ = P.normalize_procedure([{"name": "email"}], [])
    assert params == [{"name": "email", "type": "string", "required": True, "default": None, "secret": False}]


def test_param_with_default_and_types():
    params, _ = P.normalize_procedure([{"name": "n", "type": "number", "default": 3, "required": False},
                                       {"name": "flag", "type": "boolean", "default": False}], [])
    assert params[0]["default"] == 3 and params[1]["default"] is False


@pytest.mark.parametrize("params,match", [
    ([{"name": "a"}, {"name": "a"}], "duplicate"),
    ([{"name": "1bad"}], "name"),
    ([{"name": "has space"}], "name"),
    ([{"name": ""}], "name"),
    ([{"name": "a", "type": "object"}], "type"),
    ([{"name": "a", "type": "number", "default": "x"}], "default"),
    ([{"name": "a", "type": "number", "default": True}], "default"),
    ([{"name": "a", "extra": 1}], "unknown"),
    ([{"name": "a", "required": "yes"}], "required"),
    ([{"name": "a", "secret": 1}], "secret"),
    (["a"], "object"),
    ([{"name": f"p{n}"} for n in range(51)], "50"),
])
def test_bad_params_are_rejected(params, match):
    with pytest.raises(P.ProcedureError) as caught:
        P.normalize_procedure(params, [])
    assert match in str(caught.value)


def test_secret_param_stores_no_default_and_is_a_string():
    P.normalize_procedure([{"name": "pw", "secret": True}], [])
    P.normalize_procedure([{"name": "pw", "secret": True, "default": None}], [])
    with pytest.raises(P.ProcedureError):
        P.normalize_procedure([{"name": "pw", "secret": True, "default": SENTINEL}], [])
    with pytest.raises(P.ProcedureError):
        P.normalize_procedure([{"name": "pw", "secret": True, "default": ""}], [])
    with pytest.raises(P.ProcedureError):
        P.normalize_procedure([{"name": "pw", "secret": True, "type": "number"}], [])


def test_error_text_never_contains_the_value_that_was_sent():
    for params in ([{"name": "pw", "secret": True, "default": SENTINEL}], [{"name": SENTINEL + " x"}],
                   [{"name": "a", "type": SENTINEL}], [{"name": SENTINEL, "extra": SENTINEL}]):
        with pytest.raises(P.ProcedureError) as caught:
            P.normalize_procedure(params, [])
        assert SENTINEL not in str(caught.value)


# --- шаги: форма -----------------------------------------------------------------------------------------------

def test_a_full_step_is_normalized_with_all_contract_keys():
    out = norm([step()])[0]
    assert out == {"id": "s1", "action": "click", "target": {"role": "button", "name": "Next"}, "value": None,
                   "secret_ref": None, "precondition": None, "expect": None, "safe_to_retry": False, "risk": "none"}


def test_the_contract_example_step_is_valid():
    example = {"id": "s3", "action": "fill", "target": {"role": "textbox", "name": "Email"}, "value": "{{email}}",
               "secret_ref": None, "precondition": {"url_matches": "^https://example\\.com/login"},
               "expect": {"visible": {"role": "button", "name": "Войти"}}, "safe_to_retry": True, "risk": "none"}
    out = P.normalize_procedure([{"name": "email"}], [example])[1][0]
    assert out["value"] == "{{email}}" and out["safe_to_retry"] is True
    assert out["expect"] == {"visible": {"role": "button", "name": "Войти"}}


def test_step_limits():
    P.normalize_procedure([], [step(id=f"s{n}") for n in range(200)])
    bad([step(id=f"s{n}") for n in range(201)], match="200")
    bad([step(target={"role": "button", "name": "x" * 2001})], match="2000")
    bad([step(id="s1"), step(id="s1")], match="duplicate")
    bad([step(id="")], match="id")
    bad([step(id="a b")], match="id")
    bad([step(id=5)], match="id")
    bad(["click"], match="object")
    bad([step(frobnicate=1)], match="unknown")
    bad([step(action="drag")], match="action")
    bad([step(safe_to_retry="yes")], match="safe_to_retry")


@pytest.mark.parametrize("target,match", [
    (None, "target"),
    ({}, "target"),
    ({"role": "button"}, "name"),
    ({"name": "x"}, "role"),
    ({"role": "Button Bar", "name": "x"}, "role"),
    ({"role": "button", "name": 5}, "name"),
    ({"selector": ""}, "selector"),
    ({"selector": "#a", "role": "button", "name": "x"}, "target"),
    ({"url": "https://example.com/"}, "target"),
    ({"role": "button", "name": "x", "extra": 1}, "unknown"),
    ("button", "target"),
])
def test_click_target_rules(target, match):
    bad([step(target=target)], match=match)


def test_click_accepts_selector_target():
    assert norm([step(target={"selector": "#next"})])[0]["target"] == {"selector": "#next"}


def test_navigate_target_rules():
    nav = {"id": "s1", "action": "navigate", "target": {"url": "https://example.com/a"}}
    assert norm([nav])[0]["risk"] == "none"
    bad([nav | {"target": {"role": "link", "name": "x"}}], match="target")
    bad([nav | {"target": None}], match="target")
    bad([nav | {"target": {"url": "https://example.com/", "extra": 1}}], match="unknown")
    bad([nav | {"value": "x"}], match="value")


@pytest.mark.parametrize("url", ["http://127.0.0.1/", "http://169.254.169.254/latest", "http://core:8080/", "file:///etc/passwd",
                                 "javascript:alert(1)", "https://user:pw@example.com/", "http://10.0.0.5/", "http://localhost/"])
def test_navigate_url_goes_through_url_forbidden(url):
    message = bad([{"id": "s1", "action": "navigate", "target": {"url": url}}], match="url")
    assert url not in message


def test_navigate_about_blank_is_allowed_and_a_placeholder_url_is_deferred_to_the_run():
    assert norm([{"id": "s1", "action": "navigate", "target": {"url": "about:blank"}}])
    deferred = {"id": "s1", "action": "navigate", "target": {"url": "{{site}}/path"}}
    assert norm([deferred], [{"name": "site"}])[0]["target"] == {"url": "{{site}}/path"}
    assert norm([{"id": "s1", "action": "navigate", "target": {"url": "https://{{host}}/x"}}], [{"name": "host"}])


def test_press_and_wait_may_have_no_target():
    assert norm([{"id": "s1", "action": "press", "value": "Enter"}])[0]["target"] is None
    assert norm([{"id": "s1", "action": "wait", "value": "500"}])[0]["value"] == "500"
    assert norm([{"id": "s1", "action": "wait", "target": {"role": "heading", "name": "Done"}}])
    bad([{"id": "s1", "action": "press"}], match="value")
    bad([{"id": "s1", "action": "wait"}], match="wait")
    bad([{"id": "s1", "action": "wait", "value": "ten"}], match="value")
    bad([{"id": "s1", "action": "wait", "value": "600000"}], match="value")


def test_select_and_assert():
    assert norm([{"id": "s1", "action": "select", "target": {"role": "combobox", "name": "Country"}, "value": "RU"}])
    bad([{"id": "s1", "action": "select", "target": {"role": "combobox", "name": "Country"}}], match="value")
    assert norm([{"id": "s1", "action": "assert", "target": {"role": "heading", "name": "Done"}}])
    bad([{"id": "s1", "action": "assert"}], match="target")


# --- value и secret_ref ----------------------------------------------------------------------------------------

CONTEXT = {"url_matches": "^https://example\\.com/"}  # куда привели процедуру: секрет без этого не сохраняется (secret_no_context)


def fill(**fields):
    base = {"id": "s1", "action": "fill", "target": {"role": "textbox", "name": "Email"}}
    if fields.get("secret_ref") or "{{pw}}" == fields.get("value"):
        base["precondition"] = CONTEXT
    return base | fields


def test_fill_needs_value_or_secret_ref_but_not_both():
    assert norm([fill(value="a@b.c")])[0]["value"] == "a@b.c"
    assert norm([fill(secret_ref="vault:mail")])[0]["secret_ref"] == "vault:mail"
    bad([fill()], match="value")
    bad([fill(value="a", secret_ref="vault:mail")], match="exclusive")
    bad([fill(value="")], match="value")  # пустая строка не значение: пустое поле задаётся осознанно через параметр


@pytest.mark.parametrize("ref", ["mail", "vault:", "vault:1x", "vault:a b", "vault:" + "a" * 65, "VAULT:x", "vault:{{p}}"])
def test_secret_ref_format(ref):
    bad([fill(secret_ref=ref)], [{"name": "p"}], match="secret_ref")


def test_secret_ref_is_for_fill_only():
    bad([step(secret_ref="vault:x")], match="secret_ref")
    bad([{"id": "s1", "action": "press", "value": "Enter", "secret_ref": "vault:x"}], match="secret_ref")


def test_click_has_no_value():
    bad([step(value="x")], match="value")


# --- параметры в значениях -------------------------------------------------------------------------------------

def test_placeholders_must_be_declared():
    assert norm([fill(value="{{email}}")], [{"name": "email"}])
    assert norm([fill(value=" {{ email }} and {{email}}")], [{"name": "email"}])
    bad([fill(value="{{other}}")], [{"name": "email"}], match="param")
    bad([fill(value="{{other}}")], match="param")
    bad([step(target={"role": "button", "name": "{{ghost}}"})], match="param")


@pytest.mark.parametrize("value", ["{{", "}}", "{{}}", "{{a b}}", "{{1a}}", "x {{a", "{{{a}}}x}}"])
def test_stray_double_braces_are_rejected(value):
    bad([fill(value=value)], [{"name": "a"}], match="placeholder")


def test_placeholder_in_a_declared_param_name_is_case_sensitive():
    bad([fill(value="{{Email}}")], [{"name": "email"}], match="param")


def test_secret_param_only_in_the_value_of_a_fill():
    params = [{"name": "pw", "secret": True}]
    assert norm([fill(value="{{pw}}")], params)
    bad([{"id": "s1", "action": "navigate", "target": {"url": "https://example.com/?p={{pw}}"}}], params, match="secret")
    bad([step(target={"role": "button", "name": "{{pw}}"})], params, match="secret")
    bad([{"id": "s1", "action": "press", "value": "{{pw}}"}], params, match="secret")
    bad([fill(value="{{pw}}", expect={"text": "{{pw}}"})], params, match="secret")


# --- пароли ----------------------------------------------------------------------------------------------------

PASSWORD = {"role": "textbox", "name": "Password"}


def test_password_value_is_rejected_secret_ref_is_required():
    bad([fill(target=PASSWORD, value=SENTINEL)], match="secret_ref")
    assert norm([fill(target=PASSWORD, secret_ref="vault:bank")])[0]["risk"] == "login"
    for name in ("Пароль", "API key", "Token", "One-time code", "Секретный ключ"):
        bad([fill(target={"role": "textbox", "name": name}, value=SENTINEL)], match="secret_ref")
    bad([fill(target={"selector": "input[name=password]"}, value=SENTINEL)], match="secret_ref")


def test_password_value_message_does_not_echo_the_value():
    assert SENTINEL not in bad([fill(target=PASSWORD, value=SENTINEL)], match="secret_ref")


def test_password_field_accepts_only_a_secret_param_as_value():
    assert norm([fill(target=PASSWORD, value="{{pw}}")], [{"name": "pw", "secret": True}])
    bad([fill(target=PASSWORD, value="{{pw}}")], [{"name": "pw"}], match="secret_ref")
    bad([fill(target=PASSWORD, value="x{{pw}}")], [{"name": "pw", "secret": True}], match="secret_ref")


def test_plain_fields_keep_a_plain_value():
    assert norm([fill(value="hello")])[0]["risk"] == "none"


# --- риск ------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name,expected", [
    ("Pay now", "pay"), ("Оплатить", "pay"), ("Delete account", "delete"), ("Удалить", "delete"),
    ("Sign in", "login"), ("Войти", "login"), ("Submit", "send"), ("Отправить", "send"), ("Next", "none"),
])
def test_click_risk_comes_from_the_classifier(name, expected):
    assert norm([step(target={"role": "button", "name": name})])[0]["risk"] == expected


def test_selector_text_is_classified_too():
    assert norm([step(target={"selector": "#pay-button"})])[0]["risk"] == "pay"


@pytest.mark.parametrize("client", ["none", "other", "send"])
def test_client_cannot_lower_the_computed_risk(client):
    bad([step(target={"role": "button", "name": "Pay now"}, risk=client)], match="risk_below_computed")
    assert norm([step(target={"role": "button", "name": "Pay now"})])[0]["risk"] == "pay"  # без риска ядро ставит своё


def test_client_may_raise_the_risk():
    assert norm([step(risk="delete")])[0]["risk"] == "delete"
    assert norm([step(risk="other")])[0]["risk"] == "other"
    assert norm([step(target={"role": "button", "name": "Submit"}, risk="pay")])[0]["risk"] == "pay"
    assert norm([{"id": "s1", "action": "navigate", "target": {"url": "https://example.com/"}, "risk": "exec"}])[0]["risk"] == "exec"


def test_risk_order_decides_between_incomparable_labels():
    out = norm([step(target={"role": "button", "name": "Delete account"}, risk="pay")])[0]
    assert out["risk"] == "pay"  # none < other < send < push < exec < delete < login < pay
    bad([step(target={"role": "button", "name": "Pay now"}, risk="delete")], match="risk_below_computed")  # delete ниже pay


def test_unknown_risk_is_rejected():
    bad([step(risk="boom")], match="risk")
    bad([step(risk=3)], match="risk")
    assert norm([step(risk=None)])[0]["risk"] == "none"


def test_press_without_target_has_no_risk_and_with_target_uses_it():
    assert norm([{"id": "s1", "action": "press", "value": "Enter"}])[0]["risk"] == "none"
    out = norm([{"id": "s1", "action": "press", "value": "Enter", "target": {"role": "button", "name": "Pay"}}])[0]
    assert out["risk"] == "pay"


def test_fill_risk_for_ordinary_and_card_fields():
    assert norm([fill(value="x")])[0]["risk"] == "none"
    assert norm([fill(target={"role": "textbox", "name": "Card number"}, value=None, secret_ref="vault:card")])[0]["risk"] == "pay"


# --- precondition и expect -------------------------------------------------------------------------------------

def test_precondition_and_expect_shapes():
    out = norm([step(precondition={"url_matches": "^https://example\\.com/", "visible": {"role": "button", "name": "Next"}},
                     expect={"url_matches": "/done$", "visible": {"selector": "#ok"}, "text": "Готово", "timeout_ms": 9000})])[0]
    assert out["expect"]["timeout_ms"] == 9000 and out["precondition"]["visible"] == {"role": "button", "name": "Next"}
    bad([step(precondition={"text": "x"})], match="unknown")
    bad([step(expect={"timeout_ms": 10})], match="timeout_ms")
    bad([step(expect={"timeout_ms": 600000})], match="timeout_ms")
    bad([step(expect={"timeout_ms": "5000"})], match="timeout_ms")
    bad([step(expect={"visible": "x"})], match="visible")
    bad([step(expect="x")], match="expect")
    bad([step(expect={"text": 5})], match="text")


def test_empty_precondition_and_expect_become_null():
    out = norm([step(precondition={}, expect={})])[0]
    assert out["precondition"] is None and out["expect"] is None


def test_placeholders_in_expect_text_and_visible_are_checked_but_not_in_regexes():
    assert norm([step(expect={"text": "Hello {{name}}", "visible": {"role": "heading", "name": "{{name}}"}})], [{"name": "name"}])
    bad([step(expect={"text": "Hello {{name}}"})], match="param")
    bad([step(expect={"url_matches": "^https://x/{{name}}"})], [{"name": "name"}], match="url_matches")


# --- regex -----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("pattern", [
    "^https://example\\.com/login", "^https://(www\\.)?example\\.com/", "/cart/(en|ru)/", "https://[^/]+/checkout",
    "^https://example\\.com/.*checkout$", "a{1,3}b", "(?i)^HTTPS://", "^https://example\\.com/(?:a|b)/\\d+$",
])
def test_safe_regexes_pass(pattern):
    assert norm([step(expect={"url_matches": pattern}, precondition={"url_matches": pattern})])


@pytest.mark.parametrize("pattern,match", [
    ("(a+", "regex"),
    ("[a-", "regex"),
    ("x" * 301, "300"),
    ("(a+)+$", "nested"),
    ("(a*)*", "nested"),
    ("(a|aa)+$", "alternation"),
    ("(.*a){5}", "nested"),
    ("(\\w+\\s?)+", "nested"),
    ("(?:/[^/]+)*/end", "nested"),
    ("(a)\\1", "backreference"),
    (".*a.*b.*c", "repeat"),
    ("a+b+c+", "repeat"),
    ("", "url_matches"),
    (5, "url_matches"),
])
def test_dangerous_or_broken_regexes_are_rejected(pattern, match):
    for place in ("precondition", "expect"):
        bad([step(**{place: {"url_matches": pattern}})], match=match)


def test_regex_error_does_not_echo_the_pattern():
    assert SENTINEL not in bad([step(expect={"url_matches": "(" + SENTINEL})], match="regex")


# --- draft: needs_value ----------------------------------------------------------------------------------------

def test_needs_value_marks_an_unfilled_fill_and_the_procedure_is_a_draft():
    steps = norm([fill(value=None, needs_value=True)])
    assert steps[0]["needs_value"] is True and steps[0]["value"] is None and steps[0]["secret_ref"] is None
    assert P.status_for(steps) == "draft"
    assert P.status_for(norm([fill(value="x")])) == "active"
    assert "needs_value" not in norm([fill(value="x", needs_value=False)])[0]


def test_needs_value_is_only_for_an_empty_fill():
    bad([fill(value="x", needs_value=True)], match="needs_value")
    bad([fill(secret_ref="vault:a", needs_value=True)], match="needs_value")
    bad([step(needs_value=True)], match="needs_value")
    bad([fill(needs_value="yes")], match="needs_value")


def test_needs_value_on_a_password_field_is_allowed_and_secret_hint_is_kept():
    steps = norm([fill(target=PASSWORD, value=None, needs_value=True, needs_secret=True)])
    assert steps[0]["needs_secret"] is True and steps[0]["risk"] == "login"
    bad([fill(value=None, needs_secret=True)], match="needs_secret")


# --- события browser_step в шаги -------------------------------------------------------------------------------

def event(action, **fields):
    return {"action": action, "target": "", "url": None, "value": None, "result": "ok", "role": None, "name": None} | fields


def test_events_become_steps_without_snapshots_and_failures():
    events = [event("navigate", url="https://example.com/login"), event("snapshot"),
              event("fill", role="textbox", name="Email", value="[redacted]"),
              event("click", target="e5", role="button", name="Next", result="error"),
              event("click", target="e5", role="button", name="Next"), event("screenshot")]
    steps = P.events_to_steps(events)
    assert [s["action"] for s in steps] == ["navigate", "fill", "click"]
    assert [s["id"] for s in steps] == ["s1", "s2", "s3"]
    assert steps[0]["target"] == {"url": "https://example.com/login"} and steps[0]["safe_to_retry"] is True
    assert steps[2]["target"] == {"role": "button", "name": "Next"} and steps[2]["safe_to_retry"] is False
    P.normalize_procedure([], steps)


def test_fill_gets_no_value_and_needs_one():
    steps = P.events_to_steps([event("fill", role="textbox", name="Email", value="[redacted]")])
    assert steps[0]["value"] is None and steps[0]["secret_ref"] is None and steps[0]["needs_value"] is True
    assert "needs_secret" not in steps[0]
    assert P.status_for(P.normalize_procedure([], steps)[1]) == "draft"


def test_the_redacted_marker_never_becomes_a_value():
    steps = P.events_to_steps([event("fill", role="textbox", name="Email", value="[redacted]")])
    assert "[redacted]" not in str(steps)


def test_password_field_is_marked_secret():
    steps = P.events_to_steps([event("fill", role="textbox", name="Password", value="[redacted]", secret=True)])
    assert steps[0]["needs_value"] is True and steps[0]["needs_secret"] is True
    # и без пометки в событии: имя поля узнаёт тот же классификатор
    steps = P.events_to_steps([event("fill", role="textbox", name="Пароль", value="[redacted]")])
    assert steps[0]["needs_secret"] is True


def test_consecutive_navigations_to_one_address_collapse():
    events = [event("navigate", url="https://a.example/x"), event("navigate", url="https://a.example/x"),
              event("snapshot"), event("navigate", url="https://a.example/x"), event("navigate", url="https://b.example/"),
              event("click", role="link", name="Home"), event("navigate", url="https://b.example/")]
    steps = P.events_to_steps(events)
    assert [s["target"].get("url") or s["target"]["name"] for s in steps] == [
        "https://a.example/x", "https://b.example/", "Home", "https://b.example/"]


def test_a_failed_navigation_does_not_break_the_collapse_and_is_dropped():
    events = [event("navigate", url="https://a.example/"), event("navigate", url="https://a.example/", result="error"),
              event("navigate", url="https://a.example/")]
    assert len(P.events_to_steps(events)) == 1


def test_click_without_a_recorded_element_cannot_become_a_step():
    for item in (event("click", target="e5"), event("fill", value="[redacted]"), event("click", target="e5", role="button")):
        with pytest.raises(P.ProcedureError) as caught:
            P.events_to_steps([item])
        assert "target" in str(caught.value)


def test_a_click_whose_name_is_empty_keeps_the_role_and_an_empty_name():
    steps = P.events_to_steps([event("click", role="button", name="")])
    assert steps[0]["target"] == {"role": "button", "name": ""}


def test_navigation_from_an_event_that_is_not_allowed_is_not_trusted():
    steps = P.events_to_steps([event("navigate", url="http://127.0.0.1/admin")])
    with pytest.raises(P.ProcedureError):
        P.normalize_procedure([], steps)


def test_unknown_actions_and_junk_events_are_skipped():
    assert P.events_to_steps([event("hover"), {"junk": 1}, "x", None]) == []


def test_too_many_steps_from_a_turn_are_an_error():
    events = [event("click", role="button", name=f"b{n}") for n in range(201)]
    with pytest.raises(P.ProcedureError) as caught:
        P.events_to_steps(events)
    assert "200" in str(caught.value)


# --- экспорт и импорт ------------------------------------------------------------------------------------------

ROW = {"id": "11111111-1111-1111-1111-111111111111", "owner_id": "22222222-2222-2222-2222-222222222222", "bot_id": "alpha",
       "name": "Login", "description": "d", "params": [{"name": "email", "type": "string", "required": True, "default": None,
                                                         "secret": False}],
       "steps": [{"id": "s1", "action": "click", "target": {"role": "button", "name": "Next"}, "value": None,
                  "secret_ref": None, "precondition": None, "expect": None, "safe_to_retry": False, "risk": "none"}],
       "source": "bot", "status": "active", "version": 4, "created_at": "x", "updated_at": "y"}


def test_export_has_no_owner_bot_ids_or_service_fields():
    doc = P.export_document(ROW)
    assert set(doc) == {"format", "name", "description", "params", "steps"} and doc["format"] == "bothub-procedure/1"
    text = str(doc)
    for leaked in (ROW["id"], ROW["owner_id"], "alpha", "version", "source", "status"):
        assert leaked not in text.replace("secret_ref", "")


def test_import_takes_what_export_gives_and_roundtrips():
    parsed = P.parse_import(P.export_document(ROW))
    assert parsed["name"] == "Login" and parsed["steps"] == ROW["steps"] and parsed["params"] == ROW["params"]


@pytest.mark.parametrize("doc,match", [
    ({"name": "x", "format": "other/9"}, "format"),
    ({"name": "x", "owner_id": "u"}, "unknown"),
    ({"name": "x", "bot_id": "alpha"}, "unknown"),
    ({"name": "x", "id": "u"}, "unknown"),
    ({"name": "x", "version": 3}, "unknown"),
    ({"name": "x", "steps": "no"}, "steps"),
    ({"name": ""}, "name"),
    ({}, "name"),
    ({"name": "x" * 121}, "120"),
    ({"name": "x", "description": 5}, "description"),
    ({"name": "x", "description": "d" * 2001}, "description"),
    ([], "object"),
])
def test_import_rejects_foreign_ids_and_bad_shapes(doc, match):
    with pytest.raises(P.ProcedureError) as caught:
        P.parse_import(doc)
    assert match in str(caught.value)


def test_import_with_defaults_for_optional_parts():
    assert P.parse_import({"name": "x"}) == {"name": "x", "description": "", "params": [], "steps": []}


def test_name_rules():
    assert P.check_name("  Login  ") == "Login"
    assert P.check_name("x" * 120)
    for name in ("", "   ", "x" * 121, 5, None):
        with pytest.raises(P.ProcedureError):
            P.check_name(name)
