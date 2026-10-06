"""bot-image/procedure-step.mjs: исполнитель шагов процедур. Playwright подменён моделью страницы (procedure_step_harness.mjs),
настоящий main() и разбор входа работают как в контейнере. Без Node тесты пропускаются."""
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
IMAGE = HERE.parent
HARNESS = HERE / "procedure_step_harness.mjs"
SCRIPT = IMAGE / "procedure-step.mjs"
NODE = shutil.which("node")
SECRET = "hunter2-SECRET-VALUE"

BUTTON = {"role": "button", "name": "Войти", "visible": True}
FIELD = {"role": "textbox", "name": "Email", "visible": True}


def step(action="click", target=..., **extra):
    """target=... это кнопка «Войти»; None значит без цели (press, wait)."""
    return {"action": action, "target": {"role": "button", "name": "Войти"} if target is ... else target, **extra}


def envelope(step_, *, dry_run=False, deadline_ms=5000, **payload):
    return {"dry_run": dry_run, "deadline_ms": deadline_ms, "payload": {"step": step_, **payload}}


def acting(step_, world_url="https://example.com/", **payload):
    """Действие со сверкой страницы, как его шлёт ядро: адрес вкладки и origin из чтения, подпись цели."""
    target = step_.get("target")
    labelled = isinstance(target, dict) and "role" in target
    expected = {"origin": "https://example.com", "url": world_url, "role": target["role"] if labelled else None,
                "name": target["name"] if labelled else None}
    return envelope(step_, expected=expected, **payload)


def run_full(world, env=None, raw=None, timeout=20):
    body = {"world": world}
    if raw is not None:
        body["raw"] = raw
    else:
        body["envelope"] = env
    done = subprocess.run([NODE, str(HARNESS)], input=json.dumps(body), capture_output=True, text=True, timeout=timeout)
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    lines = out["output"].splitlines()
    return {"result": json.loads(lines[-1]) if lines else None, "calls": out["calls"], "output": out["output"],
            "exit_codes": out["exit_codes"], "stderr": done.stderr, "lines": len(lines), "cdp": out.get("cdp", [])}


def run(world, env=None, raw=None, timeout=20):
    out = run_full(world, env, raw, timeout)
    return out["result"], out["calls"], out["output"]


PASSWORD_FIELD = {"role": "textbox", "name": "Пароль", "visible": True}
BANK = "https://bank.example/login"
EVIL = "https://evil.example/phish"


def expected_for(url=BANK, origin="https://bank.example", role="textbox", name="Пароль"):
    return {"origin": origin, "url": url, "role": role, "name": name}


def fill_secret(**payload):
    return envelope(step("fill", {"role": "textbox", "name": "Пароль"}, value=SECRET), **payload)


@unittest.skipUnless(NODE, "нужен node")
class ProcedureStepTests(unittest.TestCase):
    def test_dry_run_reads_the_page_and_never_acts(self):
        world = {"url": "https://example.com/login?token=abc", "elements": [BUTTON]}
        result, calls, _ = run(world, envelope(step(), dry_run=True))
        self.assertTrue(result["ok"])
        self.assertIsNone(result["code"])
        self.assertIs(result["acted"], False)
        self.assertEqual(result["url"], "https://example.com/login?token=abc")
        self.assertEqual(result["found"], {"count": 1, "role": "button", "name": "Войти"})
        self.assertEqual([c["op"] for c in calls], ["connect", "disconnect"])
        self.assertEqual(calls[0]["url"], "http://127.0.0.1:9222")

    def test_execute_clicks_once_and_reports_acted(self):
        result, calls, _ = run({"url": "https://example.com/", "elements": [BUTTON]}, acting(step()))
        self.assertEqual((result["ok"], result["acted"], result["code"]), (True, True, None))
        self.assertEqual([c["op"] for c in calls if c["op"] == "click"], ["click"])

    def test_visible_precondition_is_checked_before_the_action(self):
        pre = {"visible": {"role": "heading", "name": "Вход"}}
        world = {"url": "https://example.com/", "elements": [BUTTON, {"role": "heading", "name": "Вход", "visible": False}]}
        for dry_run in (True, False):
            result, calls, _ = run(world, envelope(step(precondition=pre), dry_run=True) if dry_run else acting(step(precondition=pre)))
            self.assertEqual((result["ok"], result["code"], result["precondition_visible"], result["acted"]),
                             (False, "precondition_failed", False, False))
            self.assertFalse([c for c in calls if c["op"] == "click"])
        world["elements"][1]["visible"] = True
        result, _, _ = run(world, acting(step(precondition=pre)))
        self.assertTrue(result["ok"])
        self.assertTrue(result["precondition_visible"])

    def test_missing_and_ambiguous_targets_do_not_act(self):
        result, calls, _ = run({"url": "https://example.com/", "elements": []}, acting(step()))
        self.assertEqual((result["code"], result["acted"], result["found"]["count"]), ("element_not_found", False, 0))
        result, calls, _ = run({"url": "https://example.com/", "elements": [BUTTON, BUTTON]}, acting(step()))
        self.assertEqual((result["code"], result["acted"], result["found"]["count"]), ("element_ambiguous", False, 2))
        self.assertFalse([c for c in calls if c["op"] == "click"])

    def test_selector_target_label_comes_from_the_aria_snapshot(self):
        world = {"selectors": {"#pay": [{"visible": True, "aria": '- button "Pay \\"now\\"" [ref=e3]'}]}}
        result, _, _ = run(world, envelope(step(target={"selector": "#pay"}), dry_run=True))
        self.assertEqual(result["found"], {"count": 1, "role": "button", "name": 'Pay "now"'})
        world["selectors"]["#pay"][0]["aria"] = None
        result, _, _ = run(world, envelope(step(target={"selector": "#pay"}), dry_run=True))
        self.assertEqual(result["found"], {"count": 1, "role": None, "name": None})

    def test_dry_run_needs_no_value(self):
        result, calls, _ = run({"elements": [FIELD]}, envelope(step("fill", {"role": "textbox", "name": "Email"}), dry_run=True))
        self.assertTrue(result["ok"])
        self.assertFalse([c for c in calls if c["op"] == "fill"])

    def test_fill_types_the_value_and_never_prints_it(self):
        result, calls, output = run({"url": "https://example.com/", "elements": [FIELD]},
                                    acting(step("fill", {"role": "textbox", "name": "Email"}, value=SECRET)))
        self.assertTrue(result["ok"])
        self.assertEqual([c["value"] for c in calls if c["op"] == "fill"], [SECRET])
        self.assertNotIn(SECRET, output)

    def test_action_failure_is_a_code_without_playwright_text(self):
        world = {"url": "https://example.com/", "elements": [BUTTON], "fail": {"click": "Timeout 5000ms exceeded SECRET-PAGE-TEXT"}}
        result, _, output = run(world, acting(step()))
        self.assertEqual((result["ok"], result["code"], result["acted"]), (False, "action_failed", None))
        self.assertNotIn("SECRET-PAGE-TEXT", output)
        self.assertNotIn("Timeout", output)

    def test_navigate_checks_the_scheme_before_connecting(self):
        for url in ("file:///etc/passwd", "javascript:alert(1)", "chrome://settings", "http://a b/"):
            result, calls, _ = run({}, envelope(step("navigate", {"url": url})))
            self.assertEqual((result["code"], result["acted"]), ("url_forbidden", False), url)
            self.assertEqual(calls, [])
        result, calls, _ = run({"url": "about:blank"}, envelope(step("navigate", {"url": "https://example.com/a"})))
        self.assertEqual((result["ok"], result["acted"], result["url"]), (True, True, "https://example.com/a"))
        self.assertIn({"op": "goto", "url": "https://example.com/a"}, calls)

    def test_navigate_opens_a_page_when_there_is_none_but_probe_does_not(self):
        result, calls, _ = run({"no_page": True}, envelope(step("navigate", {"url": "https://example.com/"})))
        self.assertTrue(result["ok"])
        self.assertIn("newPage", [c["op"] for c in calls])
        result, _, _ = run({"no_page": True}, envelope(step("navigate", {"url": "https://example.com/"}), dry_run=True))
        self.assertEqual(result["code"], "no_page")
        result, _, _ = run({"no_page": True}, acting(step()))
        self.assertEqual(result["code"], "no_page")

    def test_press_select_wait_assert(self):
        world = {"url": "https://example.com/", "elements": [{"role": "combobox", "name": "Страна", "visible": True},
                              {"role": "status", "name": "ok", "visible": True, "text": "Готово: заказ 12"}]}
        result, calls, _ = run(world, acting(step("select", {"role": "combobox", "name": "Страна"}, value="Россия")))
        self.assertTrue(result["ok"])
        self.assertEqual([(c["label"], c["value"]) for c in calls if c["op"] == "select"], [("combobox|Страна", "Россия")])
        result, calls, _ = run(world, acting(step("press", None, value="Enter")))
        self.assertTrue(result["ok"])
        self.assertEqual([c["key"] for c in calls if c["op"] == "keyboard"], ["Enter"])
        result, _, _ = run(world, acting(step("assert", {"role": "status", "name": "ok"}, value="заказ 12")))
        self.assertTrue(result["ok"])
        result, _, _ = run(world, acting(step("assert", {"role": "status", "name": "ok"}, value="ошибка")))
        self.assertEqual((result["ok"], result["code"], result["acted"]), (False, "assert_failed", False))
        result, calls, _ = run(world, acting(step("wait", None, value="10")))
        self.assertTrue(result["ok"])

    def test_check_expect_reads_without_waiting(self):
        world = {"elements": [BUTTON, {"role": "heading", "name": "Готово", "visible": True}], "texts": {"Спасибо": True}}
        expect = {"visible": {"role": "heading", "name": "Готово"}, "text": "Спасибо", "url_matches": "^https://x"}
        result, _, _ = run(world, envelope(step(expect=expect), dry_run=True, check_expect=True))
        self.assertEqual(result["expect"], {"visible": True, "text": True})
        world["texts"] = {}
        world["elements"][1]["visible"] = False
        result, _, _ = run(world, envelope(step(expect=expect), dry_run=True, check_expect=True))
        self.assertEqual(result["expect"], {"visible": False, "text": False})

    def test_infrastructure_failures_are_codes(self):
        result, _, output = run({"cdp_down": True}, acting(step()))
        self.assertEqual((result["code"], result["acted"]), ("cdp_unavailable", False))
        self.assertNotIn("SECRET-PAGE-TEXT", output)
        result, _, _ = run({"no_playwright": True}, acting(step()))
        self.assertEqual((result["code"], result["acted"]), ("runtime_missing", False))

    def test_deadline_ends_with_a_code_and_unknown_outcome_while_acting(self):
        result, _, _ = run({"url": "https://example.com/", "elements": [BUTTON], "hang_click": True}, acting(step(), deadline_ms=1000))
        self.assertEqual((result["ok"], result["code"], result["acted"]), (False, "timeout", None))

    def test_invalid_input_is_rejected_without_echo(self):
        bad = [
            "not json", "[]", json.dumps({"dry_run": "yes"}),
            json.dumps(envelope(step("teleport"))),
            json.dumps(envelope(step("click", {"role": "Button", "name": "x"}))),
            json.dumps(envelope(step("click", {"role": "button", "name": "x", "extra": 1}))),
            json.dumps(envelope(step("click", {"url": "https://a.b/"}))),
            json.dumps(envelope(step("fill", {"role": "textbox", "name": "Email"}))),
            json.dumps(envelope(step("navigate", {"role": "button", "name": "x"}))),
            json.dumps(envelope(step(), deadline_ms=10)),
            json.dumps(envelope(step(), settle_ms=60000)),
            json.dumps(envelope(step(value=5))),
            json.dumps(envelope(step(precondition={"visible": {"role": "button", "name": "x"}, "bogus": 1}))),
        ]
        for raw in bad:
            result, calls, output = run({"elements": [BUTTON]}, raw=raw)
            self.assertEqual((result["ok"], result["code"], result["acted"]), (False, "invalid_input", False), raw)
            self.assertEqual(calls, [])

    def test_direct_run_answers_with_one_json_line(self):
        done = subprocess.run([NODE, str(SCRIPT)], input="garbage", capture_output=True, text=True, timeout=20)
        self.assertEqual(done.returncode, 0)
        self.assertEqual(done.stderr, "")
        self.assertEqual(json.loads(done.stdout)["code"], "invalid_input")
        good = json.dumps(envelope(step(), dry_run=True))
        done = subprocess.run([NODE, str(SCRIPT)], input=good, capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0)
        self.assertIn(json.loads(done.stdout)["code"], ("runtime_missing", "cdp_unavailable"))

    # --- пункт 2 ревью Opus: сверка страницы в одном вызове ---

    def test_the_action_goes_to_the_tab_with_the_expected_url_after_origin_and_label_checks(self):
        world = {"pages": [EVIL, BANK], "elements": [PASSWORD_FIELD]}
        out = run_full(world, fill_secret(expected=expected_for()))
        self.assertEqual((out["result"]["ok"], out["result"]["acted"], out["result"]["url"]), (True, True, BANK))
        self.assertEqual([c["page"] for c in out["calls"] if c["op"] == "fill"], [BANK])  # не на первой вкладке
        self.assertNotIn(SECRET, out["output"])

    def test_a_page_that_is_not_the_approved_one_is_changed_and_nothing_is_typed(self):
        base = {"elements": [PASSWORD_FIELD]}
        cases = {
            "нет вкладки с этим адресом": ({**base, "pages": [EVIL]}, expected_for()),
            "тот же адрес, другой origin в ожидании": ({**base, "pages": [BANK]}, expected_for(origin="https://evil.example")),
            "origin в ожидании пуст у http-страницы": ({**base, "pages": [BANK]}, expected_for(origin=None)),
            "другая роль у цели": ({**base, "pages": [BANK]}, expected_for(role="button")),
            "другое имя у цели": ({**base, "pages": [BANK]}, expected_for(name="Логин")),
        }
        for label, (world, expected) in cases.items():
            out = run_full(world, fill_secret(expected=expected))
            self.assertEqual((out["result"]["ok"], out["result"]["code"], out["result"]["acted"]), (False, "changed", False), label)
            self.assertFalse([c for c in out["calls"] if c["op"] == "fill"], label)
            self.assertNotIn(SECRET, out["output"])

    def test_an_action_without_expected_is_allowed_only_for_navigate(self):
        for action in ("click", "fill", "press", "select", "wait", "assert"):
            target = None if action in ("press", "wait") else {"role": "textbox", "name": "Email"}
            body = envelope(step(action, target, value="x" if action in ("fill", "press", "select", "wait", "assert") else None))
            result, calls, _ = run({"url": "https://example.com/", "elements": [FIELD]}, body)
            self.assertEqual((result["code"], result["acted"]), ("invalid_input", False), action)
            self.assertEqual(calls, [], action)
        result, _, _ = run({"url": "about:blank"}, envelope(step("navigate", {"url": "https://example.com/a"})))
        self.assertTrue(result["ok"])
        # чтение (dry_run) страницу по адресу не ищет и expected не требует
        result, _, _ = run({"url": "https://example.com/", "elements": [FIELD]},
                           envelope(step("click", {"role": "textbox", "name": "Email"}), dry_run=True))
        self.assertTrue(result["ok"])

    def test_malformed_expected_is_invalid_input(self):
        good = expected_for()
        for bad in ([], {"origin": "x"}, {**good, "extra": 1}, {**good, "url": 5}, {**good, "url": ""}, {**good, "role": "Button"},
                    {**good, "name": "x" * 201}, {**good, "origin": 7}):
            result, calls, _ = run({"pages": [BANK], "elements": [PASSWORD_FIELD]}, fill_secret(expected=bad))
            self.assertEqual((result["code"], result["acted"]), ("invalid_input", False), bad)
            self.assertEqual(calls, [])

    def test_after_a_secret_fill_the_address_is_read_again_and_an_origin_change_is_changed_after(self):
        world = {"url": BANK, "elements": [PASSWORD_FIELD], "url_after_fill": EVIL}
        out = run_full(world, fill_secret(expected=expected_for(), secret_input=True))
        self.assertEqual((out["result"]["ok"], out["result"]["code"], out["result"]["acted"]), (False, "changed_after", True))
        self.assertEqual(len([c for c in out["calls"] if c["op"] == "fill"]), 1)
        self.assertNotIn(SECRET, out["output"])
        same = run_full({**world, "url_after_fill": "https://bank.example/next"}, fill_secret(expected=expected_for(), secret_input=True))
        self.assertTrue(same["result"]["ok"])  # тот же origin: переход внутри сайта допустим
        plain = run_full(world, fill_secret(expected=expected_for()))  # без секрета смена origin это переход, не неизвестный исход
        self.assertEqual((plain["result"]["ok"], plain["result"]["code"], plain["result"]["acted"]), (True, None, True))
        self.assertEqual((plain["result"]["navigated"], plain["result"]["origin_changed"], plain["result"]["url"]), (True, True, EVIL))
        result, _, _ = run(world, fill_secret(expected=expected_for(), secret_input="yes"))
        self.assertEqual(result["code"], "invalid_input")

    # --- раунд 2 ревью: действие только через handle, сверка до, во время и после ---

    def test_every_targeted_action_goes_through_the_element_handle_with_a_short_timeout_and_without_force(self):
        world = {"url": "https://example.com/", "elements": [FIELD, {"role": "combobox", "name": "Страна", "visible": True}]}
        cases = [("click", {"role": "textbox", "name": "Email"}, "go", "click"), ("fill", {"role": "textbox", "name": "Email"}, "x", "fill"),
                 ("select", {"role": "combobox", "name": "Страна"}, "Россия", "select"),
                 ("press", {"role": "textbox", "name": "Email"}, "Enter", "press")]
        for action, target, value, op in cases:
            out = run_full(world, acting(step(action, target, value=value), deadline_ms=120000))
            self.assertTrue(out["result"]["ok"], action)
            ops = [c["op"] for c in out["calls"]]
            self.assertLess(ops.index("elementHandle"), ops.index(op), action)  # handle получен до действия
            done = [c for c in out["calls"] if c["op"] == op]
            self.assertEqual(len(done), 1, action)
            opts = done[0]["opts"]
            self.assertLessEqual(opts["timeout"], 2000, action)  # короткий: переход страницы не дожидается доступности
            self.assertGreater(opts["timeout"], 0, action)
            self.assertIs(opts["noWaitAfter"], True, action)
            self.assertIsNot(opts["force"], True, action)
            self.assertLessEqual(next(c for c in out["calls"] if c["op"] == "elementHandle")["opts"]["timeout"], 2000, action)

    def test_a_dry_run_takes_no_handle_and_does_not_subscribe_to_navigation(self):
        out = run_full({"url": "https://example.com/", "elements": [BUTTON]}, envelope(step(), dry_run=True))
        self.assertTrue(out["result"]["ok"])
        self.assertFalse([c for c in out["calls"] if c["op"] in ("elementHandle", "on")])

    def test_the_page_changing_between_the_check_and_the_action_never_reaches_the_action(self):
        for knob in ("url_after_handle", "url_after_handle_attached", "url_before_action"):
            for action, extra in (("fill", {"secret_input": True}), ("click", {})):
                target = {"role": "textbox", "name": "Пароль"}
                body = envelope(step(action, target, value=SECRET if action == "fill" else None), expected=expected_for(), **extra)
                out = run_full({"url": BANK, "elements": [PASSWORD_FIELD], knob: EVIL}, body)
                label = f"{knob} {action}"
                self.assertEqual((out["result"]["ok"], out["result"]["code"], out["result"]["acted"]), (False, "changed", False), label)
                self.assertFalse([c for c in out["calls"] if c["op"] in ("fill", "click")], label)
                self.assertNotIn(SECRET, out["output"], label)
                self.assertEqual(out["lines"], 1, label)

    def test_a_handle_inside_a_frame_is_changed(self):
        field = {**PASSWORD_FIELD, "in_iframe": True}
        out = run_full({"url": BANK, "elements": [field]}, fill_secret(expected=expected_for(), secret_input=True))
        self.assertEqual((out["result"]["code"], out["result"]["acted"]), ("changed", False))
        self.assertFalse([c for c in out["calls"] if c["op"] == "fill"])

    def test_an_element_that_is_not_actionable_at_the_check_is_not_acted_on_and_not_waited_for(self):
        cases = {"disabled field": ("fill", {"enabled": False}), "readonly field": ("fill", {"editable": False}),
                 "disabled button": ("click", {"enabled": False}), "hidden button": ("click", {"visible": False}),
                 "hidden assert": ("assert", {"visible": False})}
        for label, (action, flags) in cases.items():
            name = "Пароль" if action == "fill" else "Войти"
            role = "textbox" if action == "fill" else "button"
            element = {"role": role, "name": name, "visible": True, **flags}
            body = envelope(step(action, {"role": role, "name": name}, value=SECRET if action == "fill" else None),
                            expected=expected_for(role=role, name=name), secret_input=action == "fill")
            out = run_full({"url": BANK, "elements": [element]}, body)
            self.assertEqual((out["result"]["ok"], out["result"]["code"], out["result"]["acted"]),
                             (False, "stat_not_actionable", False), label)
            self.assertFalse([c for c in out["calls"] if c["op"] in ("fill", "click", "waitFor")], label)
            self.assertNotIn(SECRET, out["output"], label)

    def test_a_cross_origin_navigation_during_a_secret_fill_is_changed_after_with_the_outcome_known(self):
        body = fill_secret(expected=expected_for(), secret_input=True)
        out = run_full({"url": BANK, "elements": [PASSWORD_FIELD], "url_during_action": EVIL}, body)
        self.assertEqual((out["result"]["ok"], out["result"]["code"], out["result"]["acted"]), (False, "changed_after", True))
        self.assertEqual(len([c for c in out["calls"] if c["op"] == "fill"]), 1)  # действие одно, повтора нет
        self.assertNotIn(SECRET, out["output"])
        # действие оборвалось из-за ухода страницы: исход неизвестен
        body = fill_secret(expected=expected_for(), secret_input=True)
        out = run_full({"url": BANK, "elements": [PASSWORD_FIELD], "url_during_action": EVIL, "url_during_action_throw": True}, body)
        self.assertEqual((out["result"]["code"], out["result"]["acted"]), ("changed_after", None))
        self.assertNotIn("SECRET-PAGE-TEXT", out["output"])
        self.assertNotIn(SECRET, out["output"])

    def test_a_same_origin_navigation_during_the_action_is_fine(self):
        out = run_full({"url": BANK, "elements": [PASSWORD_FIELD], "url_during_action": "https://bank.example/next"},
                       fill_secret(expected=expected_for(), secret_input=True))
        self.assertTrue(out["result"]["ok"])
        self.assertIs(out["result"]["acted"], True)

    def test_the_navigation_listener_is_attached_for_the_action_only_and_removed_after(self):
        out = run_full({"url": "https://example.com/", "elements": [BUTTON]}, acting(step()))
        ops = [c["op"] for c in out["calls"]]
        self.assertEqual([c["event"] for c in out["calls"] if c["op"] == "on"], ["framenavigated"])
        self.assertEqual([c["event"] for c in out["calls"] if c["op"] == "off"], ["framenavigated"])
        self.assertLess(ops.index("on"), ops.index("click"))
        self.assertLess(ops.index("click"), ops.index("off"))
        failing = run_full({"url": "https://example.com/", "elements": [BUTTON], "fail": {"click": "Timeout"}}, acting(step()))
        self.assertEqual([c["op"] for c in failing["calls"] if c["op"] == "off"], ["off"])  # и при ошибке действия

    def test_a_key_press_without_a_target_checks_the_address_before_and_after(self):
        body = acting(step("press", None, value="Enter"), world_url=BANK)
        body["payload"]["expected"]["origin"] = "https://bank.example"
        ok = run_full({"url": BANK}, body)
        self.assertTrue(ok["result"]["ok"])
        self.assertEqual([c["page"] for c in ok["calls"] if c["op"] == "keyboard"], [BANK])
        self.assertLessEqual([c for c in ok["calls"] if c["op"] == "keyboard"][0]["opts"]["timeout"] or 0, 2000)
        for knob in ("url_on_subscribe", "url_on_subscribe_silent"):
            out = run_full({"url": BANK, knob: EVIL}, body)
            self.assertEqual((out["result"]["ok"], out["result"]["code"], out["result"]["acted"]), (False, "changed", False), knob)
            self.assertFalse([c for c in out["calls"] if c["op"] == "keyboard"], knob)  # нажатия не было
        out = run_full({"url": BANK, "url_during_keyboard": EVIL}, body)  # после нажатия страница ушла: это переход, не сбой
        self.assertEqual((out["result"]["ok"], out["result"]["code"], out["result"]["acted"]), (True, None, True))
        self.assertEqual((out["result"]["navigated"], out["result"]["origin_changed"], out["result"]["url"]), (True, True, EVIL))
        self.assertEqual(len([c for c in out["calls"] if c["op"] == "keyboard"]), 1)

    # --- переход после действия: успех шага, а не неизвестный исход ---

    def test_a_click_that_leaves_for_another_origin_is_ok_with_navigated_and_origin_changed(self):
        out = run_full({"url": BANK, "elements": [PASSWORD_FIELD], "url_during_action": EVIL},
                       envelope(step("click", {"role": "textbox", "name": "Пароль"}), expected=expected_for()))
        result = out["result"]
        self.assertEqual((result["ok"], result["code"], result["acted"]), (True, None, True))
        self.assertEqual((result["navigated"], result["origin_changed"]), (True, True))
        self.assertEqual(result["url"], EVIL)
        self.assertEqual(result["url_sha256"], hashlib.sha256(EVIL.encode("utf-8")).hexdigest())
        self.assertEqual(len([c for c in out["calls"] if c["op"] == "click"]), 1)  # действие одно, повтора нет
        self.assertEqual(out["lines"], 1)

    def test_press_select_and_a_plain_fill_that_leave_for_another_origin_are_ok_too(self):
        cases = [("press", "Enter"), ("select", "x"), ("fill", "x")]
        for action, value in cases:
            out = run_full({"url": BANK, "elements": [PASSWORD_FIELD], "url_during_action": EVIL},
                           envelope(step(action, {"role": "textbox", "name": "Пароль"}, value=value), expected=expected_for()))
            result = out["result"]
            self.assertEqual((result["ok"], result["code"], result["acted"], result["navigated"], result["origin_changed"]),
                             (True, None, True, True, True), action)

    def test_a_same_origin_navigation_is_navigated_without_origin_changed_and_no_navigation_is_neither(self):
        out = run_full({"url": BANK, "elements": [PASSWORD_FIELD], "url_during_action": "https://bank.example/next"},
                       envelope(step("click", {"role": "textbox", "name": "Пароль"}), expected=expected_for()))
        self.assertEqual((out["result"]["ok"], out["result"]["navigated"], out["result"]["origin_changed"]), (True, True, False))
        self.assertEqual(out["result"]["url"], "https://bank.example/next")
        still = run_full({"url": BANK, "elements": [PASSWORD_FIELD]},
                         envelope(step("click", {"role": "textbox", "name": "Пароль"}), expected=expected_for()))
        self.assertEqual((still["result"]["ok"], still["result"]["navigated"], still["result"]["origin_changed"]), (True, False, False))

    def test_a_navigation_without_an_event_is_still_seen_from_the_address_read_after_the_action(self):
        out = run_full({"url": BANK, "elements": [PASSWORD_FIELD], "url_after_fill": EVIL},
                       envelope(step("fill", {"role": "textbox", "name": "Пароль"}, value="x"), expected=expected_for()))
        self.assertEqual((out["result"]["ok"], out["result"]["navigated"], out["result"]["origin_changed"], out["result"]["url"]),
                         (True, True, True, EVIL))

    def test_a_click_that_failed_while_the_page_left_stays_an_unknown_outcome_without_page_text(self):
        body = envelope(step("click", {"role": "textbox", "name": "Пароль"}), expected=expected_for())
        out = run_full({"url": BANK, "elements": [PASSWORD_FIELD], "url_during_action": EVIL, "url_during_action_throw": True}, body)
        self.assertEqual((out["result"]["ok"], out["result"]["code"], out["result"]["acted"]), (False, "action_failed", None))
        self.assertNotIn("SECRET-PAGE-TEXT", out["output"])

    def test_every_result_carries_navigated_and_origin_changed_as_booleans(self):
        cases = ((envelope(step(), dry_run=True), {"url": "https://example.com/", "elements": [BUTTON]}),
                 (acting(step()), {"url": "https://example.com/", "elements": []}),
                 (envelope(step("navigate", {"url": "https://example.com/a"})), {"url": "about:blank"}))
        for body, world in cases:
            result = run_full(world, body)["result"]
            self.assertIs(result["navigated"], False)
            self.assertIs(result["origin_changed"], False)

    # --- переход после действия: ожидание в чтении, не в действии ---

    def test_a_read_with_settle_waits_for_the_target_and_reports_the_address_after_the_wait(self):
        world = {"url": BANK, "elements": [PASSWORD_FIELD], "url_on_wait_for": EVIL}
        read = envelope(step("click", {"role": "textbox", "name": "Пароль"}), dry_run=True, settle_ms=5000)
        out = run_full(world, read)
        self.assertEqual((out["result"]["ok"], out["result"]["url"]), (True, EVIL))  # адрес из этого же чтения, не до ожидания
        self.assertEqual(out["result"]["url_sha256"], hashlib.sha256(EVIL.encode("utf-8")).hexdigest())
        waits = [c for c in out["calls"] if c["op"] == "waitFor"]
        self.assertEqual(len(waits), 1)
        self.assertLessEqual(waits[0]["timeout"], 5000)
        loads = [c for c in out["calls"] if c["op"] == "loadState"]  # загрузка страницы ждётся в чтении, до цели, в общем пределе
        self.assertEqual([c["state"] for c in loads], ["domcontentloaded"])
        self.assertLessEqual(waits[0]["timeout"], loads[0]["timeout"])  # остаток общего settle_ms, а не второй полный срок
        self.assertLessEqual(loads[0]["timeout"], 5000)
        self.assertLess(out["calls"].index(loads[0]), out["calls"].index(waits[0]))
        # без settle чтение ничего не ждёт и показывает адрес как есть
        quick = run_full(world, envelope(step("click", {"role": "textbox", "name": "Пароль"}), dry_run=True))
        self.assertEqual(quick["result"]["url"], BANK)
        self.assertFalse([c for c in quick["calls"] if c["op"] == "waitFor"])

    def test_the_action_never_waits_for_the_target(self):
        out = run_full({"url": BANK, "elements": [PASSWORD_FIELD]},
                       envelope(step("click", {"role": "textbox", "name": "Пароль"}), expected=expected_for()))
        self.assertTrue(out["result"]["ok"])
        self.assertFalse([c for c in out["calls"] if c["op"] in ("waitFor", "loadState")])  # settle_ms в вызове действия нет: ядро шлёт 0

    # --- раунд 2 ревью: полный адрес по SHA-256 и две вкладки с одним адресом ---

    def test_the_read_reports_the_sha256_of_the_full_address_and_the_action_finds_the_tab_by_it(self):
        long_url = "https://bank.example/login?t=" + "x" * 3000
        digest = hashlib.sha256(long_url.encode("utf-8")).hexdigest()
        world = {"url": long_url, "elements": [PASSWORD_FIELD]}
        read = run_full(world, envelope(step("fill", {"role": "textbox", "name": "Пароль"}), dry_run=True))
        self.assertTrue(read["result"]["ok"])
        self.assertEqual(read["result"]["url_sha256"], digest)
        self.assertEqual(len(read["result"]["url"]), 2048)  # показ обрезан, хэш считан по полному адресу
        expected = {**expected_for(url=read["result"]["url"]), "url_sha256": digest}
        out = run_full({"pages": [long_url], "elements": [PASSWORD_FIELD]}, fill_secret(expected=expected, secret_input=True))
        self.assertEqual((out["result"]["ok"], out["result"]["acted"]), (True, True))
        # тот же показ, другой хвост адреса: хэш другой, вкладки нет
        other = "https://bank.example/login?t=" + "x" * 2990 + "y" * 10
        out = run_full({"pages": [other], "elements": [PASSWORD_FIELD]}, fill_secret(expected=expected, secret_input=True))
        self.assertEqual((out["result"]["code"], out["result"]["acted"]), ("changed", False))
        self.assertFalse([c for c in out["calls"] if c["op"] == "fill"])
        # без хэша адрес сравнивается целиком, обрезанный показ вкладку не находит
        out = run_full({"pages": [long_url], "elements": [PASSWORD_FIELD]},
                       fill_secret(expected=expected_for(url=read["result"]["url"]), secret_input=True))
        self.assertEqual((out["result"]["code"], out["result"]["acted"]), ("changed", False))

    def test_short_addresses_have_a_hash_too_and_the_expected_url_limit_is_8192(self):
        read = run_full({"url": BANK, "elements": [PASSWORD_FIELD]}, envelope(step("fill", {"role": "textbox", "name": "Пароль"}), dry_run=True))
        self.assertEqual(read["result"]["url_sha256"], hashlib.sha256(BANK.encode("utf-8")).hexdigest())
        empty = run_full({"no_page": True}, envelope(step("navigate", {"url": "https://example.com/"}), dry_run=True))
        self.assertIsNone(empty["result"]["url_sha256"])
        body = lambda url, **more: fill_secret(expected={**expected_for(url=url), **more})  # noqa: E731
        edge = "https://bank.example/" + "a" * (8192 - len("https://bank.example/"))
        for good in (body(edge), body(BANK, url_sha256="a" * 64)):
            self.assertNotEqual(run_full({"pages": [BANK], "elements": [PASSWORD_FIELD]}, good)["result"]["code"], "invalid_input")
        for bad in (body(edge + "a"), body(BANK, url_sha256="A" * 64), body(BANK, url_sha256="a" * 63), body(BANK, url_sha256=5)):
            result, calls, _ = run({"pages": [BANK], "elements": [PASSWORD_FIELD]}, bad)
            self.assertEqual((result["code"], result["acted"]), ("invalid_input", False))
            self.assertEqual(calls, [])

    def test_two_tabs_with_the_same_address_are_ambiguous_and_nothing_is_typed(self):
        world = {"pages": [BANK, BANK], "elements": [PASSWORD_FIELD]}
        out = run_full(world, fill_secret(expected=expected_for(), secret_input=True))
        self.assertEqual((out["result"]["ok"], out["result"]["code"], out["result"]["acted"]), (False, "ambiguous", False))
        self.assertFalse([c for c in out["calls"] if c["op"] in ("fill", "elementHandle")])
        self.assertNotIn(SECRET, out["output"])
        # по хэшу так же: две вкладки с одним полным адресом
        digest = hashlib.sha256(BANK.encode("utf-8")).hexdigest()
        out = run_full(world, fill_secret(expected={**expected_for(), "url_sha256": digest}, secret_input=True))
        self.assertEqual((out["result"]["code"], out["result"]["acted"]), ("ambiguous", False))
        # чтение видит то же: действие не одобряют, пока вкладок две
        read = run_full(world, envelope(step("fill", {"role": "textbox", "name": "Пароль"}), dry_run=True))
        self.assertEqual((read["result"]["ok"], read["result"]["code"]), (False, "ambiguous"))
        # navigate вкладку не сверяет и от двойников не зависит: так браузер приводят в about:blank
        nav = run_full({"pages": ["about:blank", "about:blank"]}, envelope(step("navigate", {"url": "about:blank"})))
        self.assertTrue(nav["result"]["ok"])
        self.assertEqual([c["url"] for c in nav["calls"] if c["op"] == "goto"], ["about:blank"])
        # две разные вкладки: рабочая одна, неоднозначности нет
        fine = run_full({"pages": [BANK, EVIL], "elements": [PASSWORD_FIELD]}, fill_secret(expected=expected_for(), secret_input=True))
        self.assertTrue(fine["result"]["ok"])

    # --- пункт 7: селектор только CSS ---

    def test_only_css_selectors_are_accepted_and_the_locator_is_forced_to_the_css_engine(self):
        bad = ["xpath=//input", "iframe >> internal:control=enter-frame >> input", "text=Pay", "css=#a >> nth=0", "id=pay",
               "data-testid=pay", "internal:role=button", "//button", "/html/body", "..", "a/../b", '"Pay"', "'Pay'", " XPATH=//a",
               "role=button[name=Pay]", "button >> visible=true", "", "   ",
               # псевдоклассы и псевдоэлементы Playwright (:has-text, :text, :visible, :light) и обычные (:hover) запрещены
               "a:hover", 'button:has-text("Pay")', ':text("Pay")', "#pw:visible", "*:light(#pw)", "input::placeholder",
               "li:nth-child(2)", "a:not(.x)", ":is(a)",
               # незакрытая кавычка или скобка не прячет остальной текст от проверки
               'a[href="x', "a[href=x", 'a"b >> text=Pay', "a]"]
        for selector in bad:
            result, calls, _ = run({"selectors": {selector: [{"visible": True}]}},
                                   envelope(step(target={"selector": selector}), dry_run=True))
            self.assertEqual((result["code"], result["acted"]), ("invalid_input", False), selector)
            self.assertEqual(calls, [], selector)
        for selector in ("#pay", "button.primary", "input[name=password]", "form > button", "[data-testid=pay]",
                         'a[href^="https://x"]', "a[href='http://x:80/']", "input[type=email]", r".md\:flex", "ul li a, nav a"):
            world = {"selectors": {selector: [{"visible": True, "aria": '- button "Pay"'}]}}
            out = run_full(world, envelope(step(target={"selector": selector}), dry_run=True))
            self.assertTrue(out["result"]["ok"], selector)
            self.assertIn({"op": "locator", "arg": f"css={selector}"}, out["calls"])  # движок задан явно

    # --- пункт 8: сбои самого скрипта ---

    def test_unhandled_rejection_before_the_action_is_internal_error_without_acting(self):
        out = run_full({"url": "https://example.com/", "elements": [BUTTON], "unhandled_on_connect": True}, acting(step()))
        self.assertEqual((out["result"]["ok"], out["result"]["code"], out["result"]["acted"], out["result"]["phase"]),
                         (False, "internal_error", False, "before_action"))
        self.assertEqual((out["exit_codes"], out["stderr"], out["lines"]), ([70], "", 1))
        self.assertNotIn("SECRET-PAGE-TEXT", out["output"])
        self.assertFalse([c for c in out["calls"] if c["op"] == "click"])

    def test_unhandled_rejection_and_uncaught_exception_after_the_action_started_are_unknown_outcome(self):
        for flag in ("unhandled_on_fill", "uncaught_on_fill"):
            world = {"url": BANK, "elements": [PASSWORD_FIELD], flag: True}
            out = run_full(world, fill_secret(expected=expected_for(), secret_input=True))
            result = out["result"]
            self.assertEqual((result["ok"], result["code"], result["acted"], result["phase"]), (False, "internal_error", None, "after_action"), flag)
            self.assertEqual((out["exit_codes"], out["stderr"], out["lines"]), ([70], "", 1), flag)  # одна строка, выход не 0, stderr пуст
            self.assertNotIn(SECRET, out["output"] + out["stderr"])
            self.assertNotIn("unhandled", out["output"])
            self.assertNotIn("boom", out["output"])

    def test_a_real_process_exits_nonzero_with_json_and_a_silent_stderr_on_a_rejection(self):
        script = "\n".join([
            f"import {{ main }} from {json.dumps(SCRIPT.as_uri())};",
            "const chromium = { connectOverCDP: async () => { Promise.reject(new Error('hunter2-SECRET-VALUE')); "
            "await new Promise((r) => setTimeout(r, 50)); } };",
            "const read = async () => JSON.stringify({ dry_run: true, deadline_ms: 5000, payload: { step: { action: 'wait', value: '1' } } });",
            "await main({ chromium, readInput: read });"])
        done = subprocess.run([NODE, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 70)
        self.assertEqual(done.stderr, "")
        lines = done.stdout.splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual((json.loads(lines[0])["code"], json.loads(lines[0])["phase"]), ("internal_error", "before_action"))
        self.assertNotIn(SECRET, done.stdout)


@unittest.skipUnless(NODE, "нужен node")
class RuntimeLoadingTests(unittest.TestCase):
    """Пункт 5: playwright-core грузится только по абсолютному пути из образа."""

    def probe(self, *lines, env=None, cwd=None):
        script = "\n".join([f"import * as step from {json.dumps(SCRIPT.as_uri())};", *lines])
        done = subprocess.run([NODE, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=30,
                              env={"PATH": os.environ.get("PATH", ""), **(env or {})}, cwd=cwd)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    @staticmethod
    def plant(root, marker):
        module = Path(root) / "playwright-core"
        module.mkdir(parents=True)
        (module / "index.js").write_text(f"exports.chromium = {{ marker: {json.dumps(marker)} }};\n", encoding="utf-8")
        return module / "index.js"

    def test_home_node_path_node_options_and_cwd_cannot_supply_the_module(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, node_path, cwd = Path(tmp, "home"), Path(tmp, "node_path"), Path(tmp, "cwd")
            self.plant(home / ".node_modules", "HOME")
            self.plant(home / ".node_libraries", "HOME-LIBS")
            self.plant(node_path, "NODE_PATH")
            self.plant(cwd / "node_modules", "CWD")
            env = {"HOME": str(home), "NODE_PATH": str(node_path)}
            out = self.probe("const c = step.loadPlaywright();", "console.log(JSON.stringify({ marker: c ? (c.marker ?? 'real') : null }));",
                             env=env, cwd=str(cwd))
            # настоящий playwright-core образа, если он стоит на этой машине, иначе null; подложенный не загружается никогда
            self.assertIn(out["marker"], (None, "real"))

    def test_a_missing_runtime_is_null_and_the_step_answers_runtime_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = self.probe(f"const paths = [{json.dumps(tmp + '/none/index.js')}];",
                             f"console.log(JSON.stringify({{ missing: step.loadPlaywright(paths, {{ root: {json.dumps(tmp)} }}) }}));")
            self.assertIsNone(out["missing"])
        result, _, _ = run({"no_playwright": True}, acting(step()))
        self.assertEqual((result["ok"], result["code"], result["acted"]), (False, "runtime_missing", False))

    def test_only_a_listed_absolute_file_under_the_trusted_root_is_loaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = os.path.realpath(tmp)
            good = self.plant(Path(tmp, "usr/lib/node_modules"), "IMAGE")
            outside = self.plant(Path(tmp, "elsewhere"), "OUTSIDE")
            link_dir = Path(tmp, "usr/lib/node_modules/linked")
            link_dir.mkdir()
            (link_dir / "index.js").symlink_to(outside)  # симлинк из доверенного каталога наружу
            out = self.probe(
                f"const root = {json.dumps(tmp + '/usr/')};",
                "const pick = (paths) => { const c = step.loadPlaywright(paths, { root }); return c ? c.marker : null; };",
                "console.log(JSON.stringify({",
                f"  good: pick([{json.dumps(str(good))}]),",
                f"  outside: pick([{json.dumps(str(outside))}]),",
                f"  link: pick([{json.dumps(str(link_dir / 'index.js'))}]),",
                "  relative: pick(['playwright-core/index.js', './usr/lib/node_modules/playwright-core/index.js']),",
                f"  order: pick([{json.dumps(tmp + '/usr/none.js')}, {json.dumps(str(good))}]),",
                "}));")
            self.assertEqual(out, {"good": "IMAGE", "outside": None, "link": None, "relative": None, "order": "IMAGE"})

    def test_default_paths_are_absolute_files_of_the_image(self):
        out = self.probe("console.log(JSON.stringify(step.RUNTIME_PATHS));")
        self.assertTrue(out and all(path.startswith("/usr/") and path.endswith("/playwright-core/index.js") for path in out))

    def test_source_does_not_resolve_modules_by_name_or_through_the_environment(self):
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("createRequire(root)", text)
        self.assertNotRegex(text, r"require\w*\(\s*['\"]playwright")
        self.assertNotRegex(text, r"import\(\s*['\"]playwright")
        self.assertNotIn("process.env", text)


LIVE_SKIP = None
if os.environ.get("BOTHUB_LIVE_CHROMIUM") != "1":
    LIVE_SKIP = "живой Chromium выключен: задайте BOTHUB_LIVE_CHROMIUM=1"
elif not os.environ.get("BOTHUB_PLAYWRIGHT_CORE"):
    LIVE_SKIP = "нет BOTHUB_PLAYWRIGHT_CORE (путь к каталогу playwright-core или к его index.js)"
elif not NODE:
    LIVE_SKIP = "нужен node"


@unittest.skipIf(LIVE_SKIP, LIVE_SKIP or "")
class LiveChromiumTests(unittest.TestCase):
    """Настоящий Chromium и два локальных origin (live_procedure_step.mjs): пробы оценщика J1-J5. Запуск:
    BOTHUB_LIVE_CHROMIUM=1 BOTHUB_PLAYWRIGHT_CORE=/путь/к/playwright-core [BOTHUB_CHROMIUM_EXECUTABLE=/путь/к/chrome] \
        python -m pytest -q bot-image/tests/test_procedure_step.py -k LiveChromium"""

    @classmethod
    def setUpClass(cls):
        done = subprocess.run([NODE, str(HERE / "live_procedure_step.mjs")], capture_output=True, text=True, timeout=240,
                              env={**os.environ})
        assert done.returncode == 0, done.stderr[-2000:]
        cls.out = json.loads(done.stdout.strip().splitlines()[-1])
        assert SECRET not in done.stdout

    def test_control_the_normal_page_still_works_through_the_handle(self):
        out = self.out
        self.assertEqual((out["j0_fill"]["ok"], out["j0_fill"]["acted"]), (True, True))
        self.assertEqual((out["j0_click"]["ok"], out["j0_click"]["acted"]), (True, True))
        self.assertIn({"origin": "A", "secret": True, "click": None}, out["j0_leaks"])  # значение дошло до своей страницы
        self.assertIn({"origin": "A", "secret": False, "click": "1"}, out["j0_leaks"])  # клик тоже

    def test_j1_a_field_in_a_frame_of_another_origin_is_not_found(self):
        for found in self.out["j1"]:
            self.assertEqual((found["code"], found["found"]["count"]), ("element_not_found", 0))

    def test_j2_two_tabs_with_one_address_are_ambiguous(self):
        out = self.out
        self.assertEqual((out["j2_action"]["code"], out["j2_action"]["acted"]), ("ambiguous", False))
        self.assertEqual(out["j2_read"]["code"], "ambiguous")
        self.assertEqual(out["j2_leaks"], 0)

    def test_j3_a_long_address_is_found_by_its_hash(self):
        out = self.out
        self.assertEqual(out["j3_read"], {"code": None, "url_len": 2048, "sha_ok": True})
        self.assertEqual((out["j3_action"]["ok"], out["j3_action"]["acted"], out["j3_action"]["url"]), (True, True, 2048))

    def test_j4_a_disabled_field_that_redirects_to_another_origin_never_gets_the_secret(self):
        out = self.out
        self.assertTrue(out["j4_read"]["ok"])  # чтение страницу видит
        self.assertEqual((out["j4"]["ok"], out["j4"]["code"], out["j4"]["acted"]), (False, "stat_not_actionable", False))
        self.assertLess(out["j4"]["ms"], 2500)  # не дожидалось доступности до редиректа в 800 мс и дальше
        self.assertEqual(out["j4_leaks"], [])  # ни один origin секрета не получил

    def test_j4b_a_click_on_a_disabled_button_is_not_performed_on_the_other_origin(self):
        out = self.out
        self.assertEqual((out["j4b"]["ok"], out["j4b"]["code"], out["j4b"]["acted"]), (False, "stat_not_actionable", False))
        self.assertEqual(out["j4b_leaks"], [])

    def test_j5_plain_css_passes_and_pseudo_classes_and_engines_do_not(self):
        j5 = self.out["j5"]
        for good in ("#pw", 'a[href^="https://x"]', 'input[value=".."]'):
            self.assertTrue(j5[good], good)
        for bad in ('button:has-text("Pay")', ':text("Pay")', "#pw:visible", "*:light(#pw)", "a:hover", "xpath=//a", " text=Pay",
                    "#a >> #b", "internal:control=enter-frame"):
            self.assertFalse(j5[bad], bad)


class ProcedureStepSourceTests(unittest.TestCase):
    def test_script_has_no_hidden_escapes_and_no_logging_of_input(self):
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("console.", text)
        self.assertNotIn("process.stderr", text)
        self.assertNotIn("process.env", text)
        self.assertEqual([c for c in text if ord(c) < 32 and c not in "\n"], [])

    def test_dockerfile_installs_the_script_where_the_launcher_runs_it(self):
        dockerfile = (IMAGE / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("COPY bot-image/procedure-step.mjs /usr/local/libexec/procedure-step.mjs", dockerfile)
        self.assertIn("chown root:root /usr/local/libexec/procedure-step.mjs", dockerfile)
        self.assertIn("chmod 0644 /usr/local/libexec/procedure-step.mjs", dockerfile)


if __name__ == "__main__":
    unittest.main()


class UserTabTests(unittest.TestCase):
    """Служебные поверхности Chromium (chrome://omnibox-popup… и подобные) не считаются вкладками: на живом Docker
    navigate уходил в такую «страницу» и отвечал успехом, а видимая вкладка не менялась."""

    def test_only_ordinary_pages_new_tab_and_blank_count_as_tabs(self):
        import json
        import shutil
        import subprocess
        node = shutil.which('node')
        if not node:
            self.skipTest('node not found')
        script = Path(__file__).resolve().parents[1] / 'procedure-step.mjs'
        urls = ['chrome://newtab/', 'chrome://new-tab-page/', 'about:blank', 'https://example.com/a', 'http://127.0.0.1:8080/',
                'chrome://omnibox-popup.top-chrome/', 'chrome://omnibox-popup.top-chrome/omnibox_popup_aim.html',
                'chrome://settings/', 'devtools://devtools/bundled/x.html', 'chrome-extension://abc/page.html',
                'chrome-untrusted://x/', 'chrome-error://chromewebdata/', None]
        code = (f"const m = await import({json.dumps(script.as_uri())});"
                f"console.log(JSON.stringify({json.dumps(urls)}.map((u) => m.isUserTab(u))));")
        out = subprocess.run([node, '--input-type=module', '-e', code], capture_output=True, text=True, timeout=60, check=True)
        self.assertEqual(json.loads(out.stdout), [True, True, True, True, True, False, False, False, False, False, False, False, False])


CHROME_ERROR = "chrome-error://chromewebdata/"
FRESH_BOT = {  # три CDP-цели свежего бота: фрейм у всех chrome-error (политика закрывает chrome://*), адреса целей разные
    "pages": [CHROME_ERROR, CHROME_ERROR, CHROME_ERROR],
    "targets": ["chrome://newtab/", "chrome://omnibox-popup.top-chrome/", "chrome://omnibox-popup.top-chrome/omnibox_popup_aim.html"],
}
BLANK_SHA = hashlib.sha256(b"about:blank").hexdigest()


def target_infos(out):
    return [c for c in out["cdp"] if c["method"] == "Target.getTargetInfo"]


@unittest.skipUnless(NODE, "нужен node")
class TabByTargetAddressTests(unittest.TestCase):
    """Вкладка определяется по адресу цели CDP (Target.getTargetInfo), а не по page.url(): на свежем боте у трёх «страниц»
    page.url() одинаков (chrome-error://), и любая процедура вставала на navigate с page_ambiguous."""

    def test_the_read_of_a_navigate_step_on_a_fresh_bot_is_the_blank_tab(self):
        out = run_full(FRESH_BOT, envelope(step("navigate", {"url": "https://example.com/"}), dry_run=True))
        result = out["result"]
        self.assertEqual((result["ok"], result["code"], result["acted"]), (True, None, False))
        self.assertEqual((result["url"], result["url_sha256"]), ("about:blank", BLANK_SHA))
        self.assertNotIn("newPage", [c["op"] for c in out["calls"]])
        self.assertTrue(target_infos(out))
        self.assertEqual(out["cdp"].count({"method": "detach"}), len(target_infos(out)))

    def test_navigate_with_the_expected_of_the_read_goes_into_the_blank_tab_without_ambiguity(self):
        read = run_full(FRESH_BOT, envelope(step("navigate", {"url": "https://example.com/"}), dry_run=True))["result"]
        expected = {"origin": None, "url": read["url"], "role": None, "name": None, "url_sha256": read["url_sha256"]}
        out = run_full(FRESH_BOT, envelope(step("navigate", {"url": "https://example.com/"}), expected=expected))
        result = out["result"]
        self.assertEqual((result["ok"], result["code"], result["acted"]), (True, None, True))
        self.assertEqual([c for c in out["calls"] if c["op"] == "goto"], [{"op": "goto", "url": "https://example.com/", "tab": 0}])
        # адрес и хэш результата из одного значения, прочитанного после действия
        self.assertEqual((result["url"], result["url_sha256"]),
                         ("https://example.com/", hashlib.sha256(b"https://example.com/").hexdigest()))

    def test_the_old_expected_by_page_url_does_not_match_service_surfaces(self):
        expected = {"origin": None, "url": CHROME_ERROR, "role": None, "name": None}
        out = run_full(FRESH_BOT, envelope(step("navigate", {"url": "https://example.com/"}), expected=expected))
        self.assertEqual((out["result"]["code"], out["result"]["acted"]), ("changed", False))
        self.assertFalse([c for c in out["calls"] if c["op"] == "goto"])

    def test_two_real_tabs_with_one_address_are_still_ambiguous_even_next_to_service_surfaces(self):
        world = {"pages": [CHROME_ERROR, CHROME_ERROR, CHROME_ERROR, CHROME_ERROR],
                 "targets": ["https://bank.example/login", "chrome://omnibox-popup.top-chrome/", "https://bank.example/login",
                             "chrome-extension://abc/page.html"],
                 "elements": [PASSWORD_FIELD]}
        out = run_full(world, fill_secret(expected=expected_for(), secret_input=True))
        self.assertEqual((out["result"]["code"], out["result"]["acted"]), ("ambiguous", False))
        self.assertFalse([c for c in out["calls"] if c["op"] in ("fill", "elementHandle")])
        read = run_full(world, envelope(step("fill", {"role": "textbox", "name": "Пароль"}), dry_run=True))
        self.assertEqual((read["result"]["ok"], read["result"]["code"]), (False, "ambiguous"))

    def test_a_service_surface_is_never_the_tab_even_when_it_is_first(self):
        world = {"pages": [CHROME_ERROR, "https://example.com/a"], "targets": ["chrome://omnibox-popup.top-chrome/", "https://example.com/a"],
                 "elements": [BUTTON]}
        read = run_full(world, envelope(step(), dry_run=True))["result"]
        self.assertEqual((read["ok"], read["url"]), (True, "https://example.com/a"))
        for target in ("chrome://settings/", "devtools://devtools/bundled/x.html", "chrome-extension://abc/p.html", "chrome-untrusted://x/"):
            only = {"pages": [CHROME_ERROR], "targets": [target]}
            result = run_full(only, envelope(step("navigate", {"url": "https://example.com/"}), dry_run=True))["result"]
            self.assertEqual(result["code"], "no_page", target)

    def test_the_new_tab_page_aliases_look_like_about_blank(self):
        for target in ("chrome://newtab/", "chrome://new-tab-page/"):
            result = run_full({"pages": [CHROME_ERROR], "targets": [target]},
                              envelope(step("navigate", {"url": "https://example.com/"}), dry_run=True))["result"]
            self.assertEqual((result["url"], result["url_sha256"]), ("about:blank", BLANK_SHA), target)

    def test_a_real_tab_on_an_error_page_is_page_error_for_an_action_and_the_read_not_a_silent_success(self):
        world = {"pages": [CHROME_ERROR], "targets": ["https://down.example/"], "elements": [BUTTON]}
        read = run_full(world, envelope(step(), dry_run=True))["result"]
        self.assertEqual((read["ok"], read["code"], read["acted"], read["url"]), (False, "page_error", False, "https://down.example/"))
        expected = {"origin": "https://down.example", "url": "https://down.example/", "role": "button", "name": "Войти"}
        out = run_full(world, envelope(step(), expected=expected))
        self.assertEqual((out["result"]["ok"], out["result"]["code"], out["result"]["acted"]), (False, "page_error", False))
        self.assertFalse([c for c in out["calls"] if c["op"] in ("click", "elementHandle")])
        # navigate читает такую вкладку спокойно: именно им из страницы ошибки выходят
        nav = run_full(world, envelope(step("navigate", {"url": "https://example.com/"}), dry_run=True))["result"]
        self.assertEqual((nav["ok"], nav["url"]), (True, "https://down.example/"))

    def test_a_navigate_that_ends_on_an_error_page_is_page_error_with_the_address_that_was_asked(self):
        world = {**FRESH_BOT, "goto_lands_on_error": True}
        expected = {"origin": None, "url": "about:blank", "role": None, "name": None, "url_sha256": BLANK_SHA}
        out = run_full(world, envelope(step("navigate", {"url": "https://down.example/"}), expected=expected))
        result = out["result"]
        self.assertEqual((result["ok"], result["code"], result["acted"]), (False, "page_error", True))
        self.assertEqual((result["url"], result["url_sha256"]),
                         ("https://down.example/", hashlib.sha256(b"https://down.example/").hexdigest()))

    def test_an_unreadable_target_falls_back_to_the_frame_address_and_error_pages_are_not_tabs(self):
        world = {"pages": ["https://example.com/a", CHROME_ERROR], "fail": {"cdp_session": "x"}, "elements": [BUTTON]}
        out = run_full(world, envelope(step(), dry_run=True))
        self.assertEqual((out["result"]["ok"], out["result"]["url"]), (True, "https://example.com/a"))
        self.assertNotIn("SECRET-PAGE-TEXT", out["output"])
        fresh = run_full({**FRESH_BOT, "fail": {"cdp_session": "x"}},
                         envelope(step("navigate", {"url": "https://example.com/"}), dry_run=True))
        self.assertEqual(fresh["result"]["code"], "no_page")  # цель не опознать, chrome-error:// вкладкой не считается

    def test_every_cdp_session_is_closed(self):
        out = run_full({"pages": ["https://example.com/"], "elements": [BUTTON]}, acting(step()))
        self.assertTrue(target_infos(out))
        self.assertEqual(out["cdp"].count({"method": "detach"}), len(target_infos(out)))
