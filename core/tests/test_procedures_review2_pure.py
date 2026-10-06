"""Правки по ревью Opus движка воспроизведения (пункты 2, 7, 9, 10): секрет только в приведённую страницу, селектор только CSS,
омоглифы в подписях, наследование риска у press Enter. Пробы оценщика (probe_pre.py, probe_risk.py, probe_nav.mjs) превращены в
проверки. Чистая логика `bothub.procedures` и `bothub.risk`."""
import pytest

from bothub import procedures as P
from bothub import risk

pytestmark = pytest.mark.pure

CYR_A, CYR_E, CYR_O = chr(0x430), chr(0x435), chr(0x43E)  # кириллические а, е, о: выглядят как латинские
CONTEXT = {'url_matches': r'^https://shop\.example/'}


def norm(steps, params=None, **kwargs):
    return P.normalize_procedure(params or [], steps, **kwargs)[1]


def bad(steps, params=None, match=''):
    with pytest.raises(P.ProcedureError) as caught:
        P.normalize_procedure(params or [], steps)
    assert match in str(caught.value), str(caught.value)
    return caught.value


def numbered(steps):
    return [dict(item, id=f's{n}') for n, item in enumerate(steps, 1)]


def secret_fill(name='Password', **extra):
    return {'id': 's1', 'action': 'fill', 'target': {'role': 'textbox', 'name': name}, 'secret_ref': 'vault:pw'} | extra


# --- пункт 2: секрет без привязки к странице не сохраняется ------------------------------------------------------------------

def test_a_secret_fill_needs_a_url_precondition_or_an_earlier_navigate():
    """Проба probe_pre.py: три шага, два из них раньше принимались без привязки к странице."""
    for label, target in (('role and name', {'role': 'textbox', 'name': 'Password'}), ('selector', {'selector': '#pw'})):
        error = bad([secret_fill(target=target)], match='secret_no_context')
        assert error.path == 'steps[0].precondition' and error.code == 'secret_no_context', label
    accepted = norm([secret_fill(precondition=CONTEXT)])[0]
    assert accepted['risk'] == 'login' and accepted['precondition'] == CONTEXT
    nav = {'id': 'n', 'action': 'navigate', 'target': {'url': 'https://shop.example/login'}}
    steps = norm(numbered([nav, secret_fill()]))
    assert steps[1]['secret_ref'] == 'vault:pw'
    bad(numbered([secret_fill(), nav]), match='secret_no_context')  # navigate после шага контекста не даёт
    bad([secret_fill(precondition={'visible': {'role': 'heading', 'name': 'Вход'}})], match='secret_no_context')  # нужен адрес, не заголовок


def test_a_secret_parameter_value_needs_the_same_page_context():
    params = [{'name': 'pw', 'secret': True}]
    step = {'id': 's1', 'action': 'fill', 'target': {'role': 'textbox', 'name': 'Password'}, 'value': '{{pw}}'}
    bad([step], params, match='secret_no_context')
    assert norm([dict(step, precondition=CONTEXT)], params)[0]['value'] == '{{pw}}'
    plain = [{'name': 'email'}]
    assert norm([{'id': 's1', 'action': 'fill', 'target': {'role': 'textbox', 'name': 'Email'}, 'value': '{{email}}'}], plain)  # не секрет


def test_ordinary_fills_and_a_draft_from_a_turn_need_no_context_and_a_secret_fill_is_never_safe_to_retry():
    assert norm([{'id': 's1', 'action': 'fill', 'target': {'role': 'textbox', 'name': 'Email'}, 'value': 'a@b.c'}])
    draft = {'id': 's1', 'action': 'fill', 'target': {'role': 'textbox', 'name': 'Password'}, 'value': None, 'secret_ref': None,
             'needs_value': True, 'needs_secret': True}
    assert norm([draft])[0]['needs_secret'] is True  # владелец ещё не выбрал секрет
    step = norm([secret_fill(precondition=CONTEXT, safe_to_retry=True)])[0]
    assert step['safe_to_retry'] is False  # ввод секрета повторять нельзя


# --- пункт 7: селектор только CSS --------------------------------------------------------------------------------------------

BAD_SELECTORS = ['xpath=//input', 'iframe >> internal:control=enter-frame >> input', 'text=Pay', 'css=#a >> nth=0', 'id=pay',
                 'data-testid=pay', 'internal:role=button', '//button', '/html/body', '..', 'a/../b', '"Pay"', "'Pay'",
                 ' XPATH=//a', 'role=button[name=Pay]', 'button >> visible=true',
                 # контракт: псевдоклассы и псевдоэлементы запрещены (Playwright: :has-text, :text, :visible, :light; CSS: :hover, ::before)
                 'a:hover', 'button:has-text("Pay")', ':text("Pay")', '#pw:visible', '*:light(#pw)', 'input::placeholder',
                 'ul li:nth-child(2) a', 'a:not(.x)', ':is(a)',
                 # незакрытая кавычка или скобка не прячет остальной текст от проверки
                 'a[href="x', 'a[href=x', 'a"b >> text=Pay', 'a]']
GOOD_SELECTORS = ['#pay', 'button.primary', 'input[name=password]', 'form > button', '[data-testid=pay]', 'input[type="email"]',
                  'a[href^="//cdn"]', 'a[href^="https://x"]', "a[href='http://x:80/']", 'input[value=".."]', 'input[type=email]',
                  '.md\\:flex', 'ul li a, nav a']


@pytest.mark.parametrize('selector', BAD_SELECTORS)
def test_a_non_css_selector_is_rejected_with_a_code_and_no_echo(selector):
    for step in ({'id': 's1', 'action': 'click', 'target': {'selector': selector}},
                 {'id': 's1', 'action': 'click', 'target': {'role': 'button', 'name': 'Ok'},
                  'precondition': {'visible': {'selector': selector}}},
                 {'id': 's1', 'action': 'click', 'target': {'role': 'button', 'name': 'Ok'},
                  'expect': {'visible': {'selector': selector}}}):
        error = bad([step], match='selector_not_css')
        assert error.code == 'selector_not_css' and error.path.endswith('.selector')
        assert selector.strip() not in str(error) or len(selector.strip()) < 3


@pytest.mark.parametrize('selector', GOOD_SELECTORS)
def test_a_plain_css_selector_is_accepted(selector):
    assert norm([{'id': 's1', 'action': 'click', 'target': {'selector': selector}}])[0]['target'] == {'selector': selector}


def test_css_selector_ok_matches_the_executor_rules_and_limits():
    assert not P.css_selector_ok('') and not P.css_selector_ok('   ') and not P.css_selector_ok(None)
    assert not P.css_selector_ok('a' * (P.STRING_MAX + 1)) and P.css_selector_ok('a' * P.STRING_MAX)


# --- пункт 9: омоглифы в подписях -------------------------------------------------------------------------------------------

def test_homoglyph_variants_fold_to_latin_and_to_cyrillic_without_changing_the_original():
    variants = risk.homoglyph_variants('P' + CYR_A + 'y')
    assert variants[0] == 'P' + CYR_A + 'y' and 'pay' in variants and len(set(variants)) == len(variants)
    assert risk.homoglyph_variants('Pay')[0] == 'Pay' and risk.homoglyph_variants(None) == ('',) and risk.homoglyph_variants('')[0] == ''
    assert risk.homoglyph_variants('ＰａＹ')[1] == 'pay'  # полноширинные буквы (NFKC)


@pytest.mark.parametrize('name, label', [
    ('P' + CYR_A + 'y now', 'pay'), ('Check' + CYR_O + 'ut', 'pay'), ('D' + CYR_E + 'lete account', 'delete'),
    ('S' + CYR_E + 'nd', 'send'), ('L' + CYR_O + 'g in', 'login'), ('Passw' + CYR_O + 'rd', 'login'),
    ('Опл' + 'a' + 'тить', 'pay'), ('Уд' + 'a' + 'лить', 'delete'), ('П' + 'a' + 'роль', 'login'),
    ('Next', None), ('Картинки', None), ('Sort by date', None)])
def test_a_label_with_swapped_letters_is_classified_like_the_real_word(name, label):
    labels = risk.browser_labels({'action': 'click', 'element': name})
    assert (label in labels) if label else not labels, (name, labels)


def test_the_live_risk_and_the_secret_field_check_see_through_homoglyphs():
    """Проба probe_risk.py: «Pаy» с кириллической «а» и «Оплатитьa» с латинской раньше не находились."""
    click = {'action': 'click', 'risk': 'none'}
    assert P.risk_with_live(click, 'button', 'P' + CYR_A + 'y')[0] == 'pay'
    assert P.risk_with_live(click, 'button', 'Оплатить'.replace('а', 'a'))[0] == 'pay'
    assert P.risk_with_live(click, 'button', 'Next') == ('none', [])
    assert P.risk_with_live({'action': 'fill', 'risk': 'none'}, 'textbox', 'Card numb' + CYR_E + 'r')[0] == 'pay'
    assert P.secret_field('textbox', 'П' + 'a' + 'роль') and P.secret_field('textbox', 'Passw' + CYR_O + 'rd')
    assert P.secret_field('textbox', 'Card numb' + CYR_E + 'r') and not P.secret_field('textbox', 'Email')
    steps = [{'id': 's1', 'action': 'click', 'target': {'role': 'button', 'name': 'P' + CYR_A + 'y'}}]
    assert norm(steps)[0]['risk'] == 'pay'  # и при сохранении, не только по живой подписи


def test_a_homoglyph_field_name_cannot_hide_a_secret_from_a_literal_value():
    for name in ('П' + 'a' + 'роль', 'Passw' + CYR_O + 'rd', 'Card numb' + CYR_E + 'r'):
        bad([{'id': 's1', 'action': 'fill', 'target': {'role': 'textbox', 'name': name}, 'value': 'plain'}], match='secret_required')


# --- пункт 10: press Enter наследует риск формы -----------------------------------------------------------------------------

def fill_step(name, ref):
    return {'action': 'fill', 'target': {'role': 'textbox', 'name': name}, 'secret_ref': ref, 'precondition': CONTEXT}


def test_enter_after_a_harmless_click_still_inherits_the_card_and_password_risk():
    """Проба probe_risk.py: ввод карты, «Далее» без риска, Enter: Enter раньше получал none."""
    press = {'action': 'press', 'value': 'Enter'}
    harmless = {'action': 'click', 'target': {'role': 'button', 'name': 'Next'}}
    card = norm(numbered([fill_step('Card number', 'vault:card'), harmless, press]))
    assert [s['risk'] for s in card] == ['pay', 'none', 'pay'] and card[2]['flags'] == ['login']
    password = norm(numbered([fill_step('Password', 'vault:pw'), harmless, press]))
    assert [s['risk'] for s in password] == ['login', 'none', 'login']
    for between in ({'action': 'wait', 'value': '100'}, {'action': 'press', 'value': 'Tab'}, harmless,
                    {'action': 'press', 'value': 'Tab', 'target': {'role': 'textbox', 'name': 'Next'}}):
        steps = norm(numbered([fill_step('Card number', 'vault:card'), between, press]))
        assert steps[2]['risk'] == 'pay', between


def test_only_navigate_ends_the_form_and_a_risky_click_adds_its_own_label():
    press = {'action': 'press', 'value': 'Enter'}
    nav = {'action': 'navigate', 'target': {'url': 'https://shop.example/next'}}
    steps = norm(numbered([fill_step('Password', 'vault:pw'), nav, press]))
    assert steps[2]['risk'] == 'none'
    pay = {'action': 'click', 'target': {'role': 'button', 'name': 'Pay now'}}
    after_pay = norm(numbered([{'action': 'fill', 'target': {'role': 'textbox', 'name': 'Email'}, 'value': 'a@b.c'}, pay, press]))
    assert [s['risk'] for s in after_pay] == ['none', 'pay', 'pay']
    plain = norm(numbered([{'action': 'fill', 'target': {'role': 'textbox', 'name': 'Email'}, 'value': 'a@b.c'},
                           {'action': 'click', 'target': {'role': 'button', 'name': 'Next'}}, press]))
    assert [s['risk'] for s in plain] == ['none', 'none', 'none']  # ничего рискованного: Enter тоже none


def test_computed_risk_in_responses_follows_the_same_inheritance():
    press = {'action': 'press', 'value': 'Enter'}
    harmless = {'action': 'click', 'target': {'role': 'button', 'name': 'Next'}}
    steps = norm(numbered([fill_step('Card number', 'vault:card'), harmless, press]))
    assert [s['computed_risk'] for s in P.with_computed_risk(steps)] == ['pay', 'none', 'pay']


# --- помощники подтверждения --------------------------------------------------------------------------------------------------

def test_raise_risk_keeps_the_stricter_label_and_the_rest_as_flags():
    assert P.raise_risk('none', [], 'login') == ('login', [])
    assert P.raise_risk('other', None, 'login') == ('login', [])
    assert P.raise_risk('pay', [], 'login') == ('pay', ['login'])
    assert P.raise_risk('delete', ['send'], 'login') == ('login', ['delete', 'send'])
    assert P.raise_risk('login', ['pay'], 'login') == ('pay', ['login'])
