"""Процедуры (записанные действия): проверка и нормализация шагов и параметров, риск шага, события в шаги, подстановка
параметров при запуске, сопоставление адреса с regex. Контракт: docs/contracts.md, раздел 14.

Проверка без базы и HTTP. Ошибка `ProcedureError` называет место, код причины и короткий текст: `steps[2].target.name:
param_undeclared: param is not declared`. Значения от клиента в текст не попадают: по ним в ответе нечего прочитать,
кроме собственных слов ядра."""
from __future__ import annotations

import asyncio
import atexit
import multiprocessing
import re
import unicodedata
import warnings
import weakref
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from urllib.parse import unquote, urlsplit

from bothub.browser_control import url_forbidden
from bothub.risk import browser_labels, homoglyph_variants

try:  # Python 3.11+: re._parser; раньше sre_parse
    import re._constants as _sc
    import re._parser as _sp
except ImportError:  # pragma: no cover
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', DeprecationWarning)
        import sre_constants as _sc
        import sre_parse as _sp

STEPS_MAX = 200
PARAMS_MAX = 50
NAME_MAX = 120
DESCRIPTION_MAX = 2000
STRING_MAX = 2000  # любая строка шага
REGEX_MAX = 300
REGEX_UNBOUNDED_HIGH_LOW = 10  # повтор с high - low больше этого считается неограниченным
REGEX_UNBOUNDED_MAX = 1  # неограниченных повторов в шаблоне: больше даёт полиномиальный перебор на длинном адресе
REGEX_BUDGET_MAX = 200  # сумма верхних границ ограниченных повторов
MATCH_URL_MAX = 2048  # адрес режется до этой длины перед сопоставлением
MATCH_URL_TIMEOUT = 0.2  # секунд на одно сопоставление, потом процесс убивается
TIMEOUT_MIN, TIMEOUT_MAX = 100, 60000
WAIT_MAX = 60000
FORMAT = 'bothub-procedure/1'

ACTIONS = ('navigate', 'click', 'fill', 'press', 'select', 'wait', 'assert')
RISKS = ('none', 'pay', 'send', 'delete', 'login', 'push', 'exec', 'other')
# Порядок для «клиент может только повысить». Метки несравнимы по смыслу, порядок задан явно: жёсткие (delete, login, pay)
# выше остальных, pay выше всех, чтобы метка оплаты не терялась.
RISK_ORDER = ('none', 'other', 'send', 'push', 'exec', 'delete', 'login', 'pay')
PARAM_TYPES = ('string', 'number', 'boolean')
SUBMIT_KEYS = frozenset({'enter', 'numpadenter', 'space', ' '})  # press этими клавишами отправляет форму
PAY_URL_WORDS = frozenset({'checkout', 'payment', 'payments', 'pay', 'oplata', 'оплата', 'billing'})

_STEP_KEYS = frozenset({'id', 'action', 'target', 'value', 'secret_ref', 'precondition', 'expect', 'safe_to_retry', 'risk',
                        'needs_value', 'needs_secret', 'flags', 'computed_risk'})  # flags и computed_risk ядро считает само
_PARAM_KEYS = frozenset({'name', 'type', 'required', 'default', 'secret'})
_PARAM_NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_]{0,63}')
_STEP_ID = re.compile(r'[A-Za-z0-9_-]{1,64}')
_ROLE = re.compile(r'[a-z][a-z0-9-]{0,63}')
_SECRET_REF = re.compile(r'vault:([A-Za-z][A-Za-z0-9_-]{0,63})')
_PLACEHOLDER = re.compile(r'\{\{\s*([A-Za-z_][A-Za-z0-9_]{0,63})\s*\}\}')
_PLACEHOLDER_ONLY = re.compile(r'\s*\{\{\s*([A-Za-z_][A-Za-z0-9_]{0,63})\s*\}\}\s*')
_DIGITS = re.compile(r'[0-9]{1,6}')
_URL_WORDS = re.compile(r'[^0-9a-zа-яё]+')


class ProcedureError(ValueError):
    """Процедура не принята. Текст: `место: код: причина`, без значений от клиента."""

    def __init__(self, path: str, code: str, text: str):
        super().__init__(f'{path}: {code}: {text}')
        self.path, self.code, self.text = path, code, text


def _fail(path: str, code: str, text: str):
    raise ProcedureError(path, code, text)


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _text(path: str, value, *, maximum: int = STRING_MAX, minimum: int = 0) -> str:
    if not isinstance(value, str):
        _fail(path, 'type', 'must be a string')
    if len(value) > maximum:
        _fail(path, 'too_long', f'exceeds {maximum} characters')
    if len(value) < minimum:
        _fail(path, 'empty', 'is empty')
    return value


def _object(path: str, value, allowed) -> dict:
    if not isinstance(value, dict):
        _fail(path, 'type', 'must be an object')
    if set(value) - set(allowed):
        _fail(path, 'unknown_field', 'unknown field')
    return value


# --- невидимые символы --------------------------------------------------------------------------------------------

def strip_invisible(text) -> str:
    """Без управляющих (Cc) и невидимых форматирующих (Cf) символов, кроме обычного пробела: переводы строки, NUL, bidi
    (U+202E, U+2066...), нулевой ширины, мягкий перенос, BOM. Неразрывный пробел (Zs) остаётся."""
    return ''.join(char for char in str(text or '') if char == ' ' or unicodedata.category(char) not in ('Cc', 'Cf'))


def _has_invisible(text: str) -> bool:
    return any(char != ' ' and unicodedata.category(char) in ('Cc', 'Cf') for char in text)


# --- имя, описание ----------------------------------------------------------------------------------------------

def check_name(value) -> str:
    if not isinstance(value, str):
        _fail('name', 'type', 'must be a string')
    value = value.strip()
    if not value:
        _fail('name', 'empty', 'is empty')
    if len(value) > NAME_MAX:
        _fail('name', 'too_long', f'exceeds {NAME_MAX} characters')
    return value


def check_description(value) -> str:
    if not isinstance(value, str):
        _fail('description', 'type', 'must be a string')
    if len(value) > DESCRIPTION_MAX:
        _fail('description', 'too_long', f'exceeds {DESCRIPTION_MAX} characters')
    return value


# --- параметры --------------------------------------------------------------------------------------------------

def normalize_params(params) -> list[dict]:
    if not isinstance(params, list):
        _fail('params', 'type', 'must be an array')
    if len(params) > PARAMS_MAX:
        _fail('params', 'too_many', f'at most {PARAMS_MAX} params')
    out, seen = [], set()
    for index, raw in enumerate(params):
        path = f'params[{index}]'
        raw = _object(path, raw, _PARAM_KEYS)
        name = raw.get('name')
        if not isinstance(name, str) or not _PARAM_NAME.fullmatch(name):
            _fail(f'{path}.name', 'invalid', 'invalid name')
        if name in seen:
            _fail(f'{path}.name', 'duplicate', 'duplicate name')
        seen.add(name)
        kind = raw.get('type', 'string')
        if not isinstance(kind, str) or kind not in PARAM_TYPES:
            _fail(f'{path}.type', 'invalid', 'unknown type')
        required = raw.get('required', True)
        secret = raw.get('secret', False)
        if not isinstance(required, bool):
            _fail(f'{path}.required', 'type', 'must be a boolean')
        if not isinstance(secret, bool):
            _fail(f'{path}.secret', 'type', 'must be a boolean')
        default = raw.get('default')
        if secret:
            if kind != 'string':
                _fail(f'{path}.secret', 'invalid', 'a secret param is a string')
            if default is not None:
                _fail(f'{path}.default', 'not_allowed', 'a secret param stores no default')
        elif default is not None:
            ok = (isinstance(default, str) if kind == 'string' else isinstance(default, bool) if kind == 'boolean'
                  else (isinstance(default, (int, float)) and not isinstance(default, bool)))
            if not ok:
                _fail(f'{path}.default', 'type', f'does not match type {kind}')
            if isinstance(default, str):
                _text(f'{path}.default', default)
        out.append({'name': name, 'type': kind, 'required': required, 'default': default, 'secret': secret})
    return out


# --- regex ------------------------------------------------------------------------------------------------------

def _is_repeat(op) -> bool:
    return op in (_sc.MAX_REPEAT, _sc.MIN_REPEAT) or op == getattr(_sc, 'POSSESSIVE_REPEAT', None)


def _children(op, av):
    """Вложенные подшаблоны узла разбора."""
    if _is_repeat(op):
        yield av[2]
    elif op == _sc.SUBPATTERN:
        yield av[3]
    elif op == _sc.BRANCH:
        yield from av[1]
    elif op == getattr(_sc, 'ATOMIC_GROUP', None):
        yield av


def _inspect_regex(pattern) -> str | None:
    """Код причины, если шаблон опасен для перебора, иначе None. Правила:
    - обратных ссылок, условных групп и lookaround нет;
    - повтор с high - low > REGEX_UNBOUNDED_HIGH_LOW считается неограниченным (`*`, `+`, `{n,}`, `{0,200}`); такой повтор в
      шаблоне не больше REGEX_UNBOUNDED_MAX и он не внутри группы с повтором;
    - внутри группы, которая может повториться больше одного раза, нет ни повтора, ни альтернативы;
    - сумма верхних границ ограниченных повторов не больше REGEX_BUDGET_MAX (`?` это 1, `{3}` это 3)."""
    unbounded = budget = 0
    stack = [(pattern, False)]  # (подшаблон, лежит ли внутри группы, которая может повториться больше одного раза)
    while stack:
        sub, repeating = stack.pop()
        for op, av in sub:
            if op in (_sc.GROUPREF, _sc.GROUPREF_EXISTS):
                return 'regex_backreference'
            if op in (_sc.ASSERT, _sc.ASSERT_NOT):
                return 'regex_lookaround'
            if _is_repeat(op):
                low, high = av[0], av[1]
                if repeating:
                    return 'regex_nested'
                if high - low > REGEX_UNBOUNDED_HIGH_LOW:
                    unbounded += 1
                    if unbounded > REGEX_UNBOUNDED_MAX:
                        return 'regex_unbounded'
                else:
                    budget += high
                inner = high > 1
            else:
                if repeating and op == _sc.BRANCH:
                    return 'regex_alternation'
                inner = repeating
            stack.extend((child, inner) for child in _children(op, av))
    return 'regex_budget' if budget > REGEX_BUDGET_MAX else None


_REGEX_TEXT = {
    'regex_nested': 'a repeat inside a repeated group is not allowed',
    'regex_alternation': 'alternation inside a repeated group is not allowed',
    'regex_backreference': 'backreferences and conditional groups are not allowed',
    'regex_lookaround': 'lookahead and lookbehind are not allowed',
    'regex_unbounded': f'at most {REGEX_UNBOUNDED_MAX} unbounded repeat',
    'regex_budget': f'repeat bounds add up to more than {REGEX_BUDGET_MAX}',
}


def check_regex(path: str, pattern) -> str:
    """Regex адреса: строка до REGEX_MAX, компилируется, проходит статическое правило `_inspect_regex`. У `re` нет
    прерывания, поэтому правило режет известные классы катастрофического перебора при сохранении (`(a+)+`, `(a|aa)*`,
    `(.*a){n}`), а сопоставление при запуске идёт отдельным процессом с пределом времени (`match_url`)."""
    if not isinstance(pattern, str) or not pattern:
        _fail(path, 'invalid', 'must be a non-empty string')
    if len(pattern) > REGEX_MAX:
        _fail(path, 'regex_too_long', f'regex exceeds {REGEX_MAX} characters')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            parsed = _sp.parse(pattern)
            re.compile(pattern)
    except (re.error, RecursionError, OverflowError, ValueError):
        _fail(path, 'regex_invalid', 'invalid regex')
    reason = _inspect_regex(parsed)
    if reason:
        _fail(path, reason, _REGEX_TEXT[reason])
    return pattern


# --- сопоставление адреса с regex: отдельный процесс, предел времени -------------------------------------------

def _match_worker(pattern: str, url: str) -> bool:  # выполняется в дочернем процессе
    try:
        return re.search(pattern, url) is not None
    except (re.error, RecursionError, OverflowError, ValueError):
        return False


def _match_ping() -> bool:
    return True


_pool: ProcessPoolExecutor | None = None
_locks: 'weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]' = weakref.WeakKeyDictionary()


def _new_pool() -> ProcessPoolExecutor:
    """Один воркер; прогрет до первого сопоставления, чтобы запуск процесса не засчитывался в предел времени."""
    pool = ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context('spawn'))
    try:
        pool.submit(_match_ping).result(30)
    except BaseException:
        _kill_pool(pool)
        raise
    return pool


def _kill_pool(pool: ProcessPoolExecutor | None) -> None:
    if pool is None:
        return
    for process in list((getattr(pool, '_processes', None) or {}).values()):
        try:
            process.kill()
        except Exception:  # процесс уже завершился
            pass
    pool.shutdown(wait=False, cancel_futures=True)


def shutdown_match_pool() -> None:
    global _pool
    pool, _pool = _pool, None
    _kill_pool(pool)


atexit.register(shutdown_match_pool)


async def match_url(pattern: str, url: str) -> tuple[bool, bool]:
    """`(совпало, таймаут)`. Адрес режется до MATCH_URL_MAX символов, сопоставление идёт в отдельном процессе (один воркер
    `ProcessPoolExecutor`); дольше MATCH_URL_TIMEOUT секунд: процесс убивается, результат `(False, True)`, следующий вызов
    поднимает новый. Event loop не блокируется. Шаблон должен пройти `check_regex` при сохранении, но и тяжёлый шаблон
    здесь укладывается в предел. Битый шаблон: `(False, False)`."""
    global _pool
    url = str(url)[:MATCH_URL_MAX]
    loop = asyncio.get_running_loop()
    lock = _locks.get(loop)
    if lock is None:
        lock = _locks[loop] = asyncio.Lock()
    async with lock:  # один воркер: очередь не должна съедать предел времени соседа
        if _pool is None:
            try:
                _pool = await loop.run_in_executor(None, _new_pool)
            except Exception:
                return False, True
        try:
            future = _pool.submit(_match_worker, pattern, url)
            return bool(await asyncio.wait_for(asyncio.wrap_future(future), MATCH_URL_TIMEOUT)), False
        except (asyncio.TimeoutError, BrokenProcessPool, RuntimeError):
            pool, _pool = _pool, None
            _kill_pool(pool)
            return False, True


# --- параметры в строках ----------------------------------------------------------------------------------------

def _check_placeholders(path: str, text: str, params: dict, *, secret_ok: bool = False) -> None:
    rest = _PLACEHOLDER.sub('', text)
    if '{{' in rest or '}}' in rest:
        _fail(path, 'placeholder_invalid', 'invalid placeholder')
    for match in _PLACEHOLDER.finditer(text):
        param = params.get(match.group(1))
        if param is None:
            _fail(path, 'param_undeclared', 'param is not declared')
        if param['secret'] and not secret_ok:
            _fail(path, 'secret_param_misplaced', 'a secret param is allowed only as the value of a fill')


def _has_placeholder(text: str) -> bool:
    return '{{' in text or '}}' in text


# --- цели -------------------------------------------------------------------------------------------------------

# Селектор только обычный CSS. Движки Playwright (`xpath=`, `text=`, `css=`, `id=`, `data-testid=`, `internal:control=enter-frame`),
# цепочка `>>`, xpath (`//`, `..`, ведущая `/`), текст в кавычках и ПСЕВДОКЛАССЫ с псевдоэлементами (`:has-text`, `:text`, `:visible`,
# `:light`, `:hover`, `::before`) по цели не дают судить о риске и выходят за контракт. Строки в кавычках и содержимое `[..]`
# (`a[href^="https://x"]`), а также экранированное `\:` (`md\:flex`) в проверке не участвуют. Незакрытая кавычка или скобка:
# отказ. Те же правила в `bot-image/procedure-step.mjs` (`cssSelectorOk`); исполнитель вызывает `page.locator('css=' + селектор)`.
_SELECTOR_ENGINE = re.compile(r'\s*(?:[a-z][a-z0-9_-]*\s*=|internal:)', re.I)


def _selector_skeleton(selector: str) -> str | None:
    """Селектор без строк в кавычках, содержимого `[..]` и экранированных символов; None: кавычки или скобки не сошлись."""
    out: list[str] = []
    quote = None
    depth = 0
    index = 0
    while index < len(selector):
        char = selector[index]
        if char == '\\':
            index += 2
            out.append('x')
            continue
        index += 1
        if quote:
            if char == quote:
                quote = None
            continue
        if char in ('"', "'"):
            quote = char
            continue
        if char == '[':
            depth += 1
        elif char == ']':
            if depth == 0:
                return None
            depth -= 1
        if depth == 0 or char in '[]':
            out.append(char)
    return ''.join(out) if quote is None and depth == 0 else None


def css_selector_ok(selector: str) -> bool:
    if not isinstance(selector, str) or not selector.strip() or len(selector) > STRING_MAX:
        return False
    head = selector.lstrip()
    if head[:1] in ('/', '"', "'", '`') or _SELECTOR_ENGINE.match(selector) is not None:
        return False
    skeleton = _selector_skeleton(selector)
    return skeleton is not None and not any(bad in skeleton for bad in ('>>', '//', '..', ':'))


def _target(path: str, raw, params: dict) -> dict:
    """{"role","name"} или {"selector"}. Параметр допустим только в name: селектор с параметром отклоняется (по селектору
    риск не определить), невидимые и управляющие символы в роли и имени отклоняются."""
    raw = _object(path, raw, ('role', 'name', 'selector'))
    if 'selector' in raw:
        if 'role' in raw or 'name' in raw:
            _fail(path, 'invalid', 'either role and name or selector')
        selector = _text(f'{path}.selector', raw['selector'], minimum=1)
        if _has_placeholder(selector):
            _fail(f'{path}.selector', 'param_in_selector', 'a param in a selector is not allowed')
        if not css_selector_ok(selector):
            _fail(f'{path}.selector', 'selector_not_css', 'only a plain CSS selector is allowed')
        return {'selector': selector}
    if 'role' not in raw:
        _fail(f'{path}.role', 'required', 'is required')
    if 'name' not in raw:
        _fail(f'{path}.name', 'required', 'is required')
    role = raw['role']
    if not isinstance(role, str) or not _ROLE.fullmatch(role):
        _fail(f'{path}.role', 'invalid', 'invalid role')
    name = _text(f'{path}.name', raw['name'])
    if _has_invisible(name):
        _fail(f'{path}.name', 'control_chars', 'control and invisible characters are not allowed')
    _check_placeholders(f'{path}.name', name, params)
    return {'role': role, 'name': name}


def _condition(path: str, raw, params: dict, keys) -> dict | None:
    raw = _object(path, raw, keys)
    out = {}
    if raw.get('url_matches') is not None:  # null это то же, что нет поля, как у visible
        pattern = check_regex(f'{path}.url_matches', raw['url_matches'])
        if _has_placeholder(pattern):
            _fail(f'{path}.url_matches', 'param_in_regex', 'placeholders are not substituted in a regex')
        out['url_matches'] = pattern
    if raw.get('visible') is not None:
        out['visible'] = _target(f'{path}.visible', raw['visible'], params)
    if 'text' in keys and raw.get('text') is not None:
        text = _text(f'{path}.text', raw['text'], minimum=1)
        _check_placeholders(f'{path}.text', text, params)
        out['text'] = text
    if 'timeout_ms' in keys and raw.get('timeout_ms') is not None:
        timeout = raw['timeout_ms']
        if not _is_int(timeout) or not TIMEOUT_MIN <= timeout <= TIMEOUT_MAX:
            _fail(f'{path}.timeout_ms', 'invalid', f'must be an integer from {TIMEOUT_MIN} to {TIMEOUT_MAX}')
        out['timeout_ms'] = timeout
    return out or None


# --- поле как секрет --------------------------------------------------------------------------------------------

def _squash(text) -> str:
    """NFKC, без пробелов, дефисов и подчёркиваний, в нижнем регистре: «C V V», «Card-Number», «MOT DE PASSE»."""
    text = unicodedata.normalize('NFKC', str(text or ''))
    return ''.join(char for char in text if not char.isspace() and char not in '-_').casefold()


# Платёжные данные и коды, которые классификатор риска не называет паролем (он знает password, token, otp...).
_SECRET_WORDS = tuple(_squash(word) for word in (
    'cvv', 'cvc', 'card number', 'номер карты', 'срок действия', 'security code', 'код подтверждения', 'одноразовый код',
    'otp', '2fa', 'passwort', 'kennwort', 'contraseña', 'mot de passe', 'senha', 'пароль'))


def _classifier_args(action: str, target: dict | None) -> dict:
    args = {'action': 'fill' if action == 'fill' else 'click'}
    if 'selector' in target:
        args['target'] = target['selector']
    else:
        args['element'] = target['name']
        args['page_label'] = f"{target['role']} {target['name']}".strip()
    return args


def _secret_target(target: dict | None) -> bool:
    """Поле для пароля, токена, кода, данных карты: значение туда в процедуре не хранится. Невидимые символы и bidi
    сначала убираются, потом работают классификатор риска (`login`) и список слов (после NFKC, без пробелов)."""
    if not target:
        return False
    if 'selector' in target:
        text = strip_invisible(target['selector'])
        clean = {'selector': text}
    else:
        text = strip_invisible(target.get('name'))
        clean = {'role': strip_invisible(target.get('role')), 'name': text}
    if 'login' in browser_labels(_classifier_args('fill', clean)):
        return True
    return any(word in squashed for squashed in map(_squash, homoglyph_variants(text)) for word in _SECRET_WORDS)


def secret_field(role, name) -> bool:
    return _secret_target({'role': str(role or ''), 'name': str(name or '')})


# --- риск -------------------------------------------------------------------------------------------------------

def _rank(label: str) -> int:
    return RISK_ORDER.index(label)


def _pay_url(url: str) -> bool:
    return any(word in PAY_URL_WORDS for word in _URL_WORDS.split(unquote(url).lower()))


def _own_labels(step: dict) -> set[str]:
    """Метки риска самого шага без наследования. `other`: цель с параметром в имени или адресе, fill в безымянное поле и
    в поле по селектору (что там, не видно). Остальные метки даёт классификатор живых действий браузера."""
    action, target = step['action'], step.get('target')
    labels: set[str] = set()
    if action == 'navigate':
        url = target['url']
        if _has_placeholder(url):
            labels.add('other')
        elif _pay_url(url):
            labels.add('pay')
        return labels
    if action not in ('click', 'fill', 'press', 'select') or not target:
        return labels
    if 'selector' in target:
        if action == 'fill':
            labels.add('other')
    else:
        if _has_placeholder(target['name']) or (action == 'fill' and not strip_invisible(target['name']).strip()):
            labels.add('other')
    labels.update(browser_labels(_classifier_args(action, target)))
    if action == 'fill' and _secret_target(target):
        labels.add('login')
    return labels


def _submits(step: dict) -> bool:
    return step['action'] == 'press' and not step.get('target') and (
        _has_placeholder(step['value']) or step['value'].strip().lower() in SUBMIT_KEYS or step['value'] == ' ')


def _step_labels(steps: list[dict]) -> list[set[str]]:
    """Метки по шагам с наследованием: press Enter, NumpadEnter, Space без цели отправляет форму, поэтому получает метки
    всего, что шло в страницу с последнего navigate: fill, select, click и press с целью. Click без риска форму не
    «закрывает»: где кончается форма, по записи не видно (карту ввели, нажали «Далее», Enter отправил платёж). Сбрасывает
    наследование только navigate (новая страница)."""
    form: set[str] = set()
    out = []
    for step in steps:
        labels = _own_labels(step)
        if _submits(step):
            labels = labels | form
        out.append(labels)
        if step['action'] == 'navigate':
            form = set()
        elif step['action'] in ('fill', 'select', 'click') or (step['action'] == 'press' and step.get('target')):
            form = form | labels
    return out


def _split(labels: set[str], floor: str = 'none') -> tuple[str, list[str]]:
    """Строгая метка в риск, остальные жёсткие в flags. floor: риск не ниже него (поднятый клиентом)."""
    ordered = sorted((label for label in labels | {floor} if label in RISK_ORDER and label != 'none'), key=_rank, reverse=True)
    if not ordered:
        return 'none', []
    return ordered[0], [label for label in ordered[1:] if label != 'other']


def with_computed_risk(steps: list[dict]) -> list[dict]:
    """Копии шагов с `computed_risk`: риск, который вычислило ядро, без повышения клиентом. Клиент не может опустить риск
    ниже него (PWA берёт его нижней границей выбора)."""
    return [dict(step, computed_risk=_split(labels)[0]) for step, labels in zip(steps, _step_labels(steps))]


# --- шаги -------------------------------------------------------------------------------------------------------

def _step(index: int, raw, params: dict) -> tuple[dict, str | None]:
    path = f'steps[{index}]'
    raw = _object(path, raw, _STEP_KEYS)
    step_id = raw.get('id')
    if not isinstance(step_id, str) or not _STEP_ID.fullmatch(step_id):
        _fail(f'{path}.id', 'invalid', 'invalid id')
    action = raw.get('action')
    if not isinstance(action, str) or action not in ACTIONS:
        _fail(f'{path}.action', 'invalid', 'unknown action')
    safe = raw.get('safe_to_retry', False)
    if safe is None:
        safe = False
    if not isinstance(safe, bool):
        _fail(f'{path}.safe_to_retry', 'type', 'must be a boolean')
    requested = raw.get('risk')
    if requested is not None and (not isinstance(requested, str) or requested not in RISKS):
        _fail(f'{path}.risk', 'risk_invalid', 'unknown risk')

    # цель
    given = raw.get('target')
    if action == 'navigate':
        if not isinstance(given, dict):
            _fail(f'{path}.target', 'required', 'is required: {url}')
        given = _object(f'{path}.target', given, ('url',))
        url = _text(f'{path}.target.url', given.get('url'), minimum=1)
        _check_placeholders(f'{path}.target.url', url, params)
        if not _has_placeholder(url) and (reason := url_forbidden(url)):
            _fail(f'{path}.target.url', 'url_forbidden', f'forbidden ({reason})')  # адрес с параметром проверит запуск
        target = {'url': url}
    elif given is None:
        if action in ('click', 'fill', 'select', 'assert'):
            _fail(f'{path}.target', 'required', 'is required')
        target = None
    else:
        target = _target(f'{path}.target', given, params)

    # value и secret_ref
    value, secret_ref = raw.get('value'), raw.get('secret_ref')
    needs_value, needs_secret = raw.get('needs_value', False), raw.get('needs_secret', False)
    if not isinstance(needs_value, bool):
        _fail(f'{path}.needs_value', 'type', 'must be a boolean')
    if not isinstance(needs_secret, bool):
        _fail(f'{path}.needs_secret', 'type', 'must be a boolean')
    if needs_value and (action != 'fill' or value is not None or secret_ref is not None):
        _fail(f'{path}.needs_value', 'invalid', 'only for a fill without value and secret_ref')
    if needs_secret and not needs_value:
        _fail(f'{path}.needs_secret', 'invalid', 'only together with needs_value')
    if secret_ref is not None and action != 'fill':
        _fail(f'{path}.secret_ref', 'not_allowed', 'only for fill')
    if value is not None and action in ('navigate', 'click'):
        _fail(f'{path}.value', 'not_allowed', f'not allowed for {action}')
    if value is not None and secret_ref is not None:
        _fail(f'{path}.value', 'mutually_exclusive', 'value and secret_ref are mutually exclusive')
    if value is not None:
        _text(f'{path}.value', value, minimum=1)
        _check_placeholders(f'{path}.value', value, params, secret_ok=action == 'fill')
    if secret_ref is not None:
        if not isinstance(secret_ref, str) or not _SECRET_REF.fullmatch(secret_ref):
            _fail(f'{path}.secret_ref', 'invalid', 'expected vault:<name>')
    if action in ('press', 'select') and value is None:
        _fail(f'{path}.value', 'required', 'is required')
    if action == 'wait':
        if target is None and value is None:
            _fail(path, 'required', 'wait needs a target or a value')
        if value is not None and not _has_placeholder(value) and (
                not _DIGITS.fullmatch(value) or int(value) > WAIT_MAX):
            _fail(f'{path}.value', 'invalid', f'wait expects milliseconds up to {WAIT_MAX}')
    if action == 'fill' and not needs_value and value is None and secret_ref is None:
        _fail(f'{path}.value', 'required', 'value or secret_ref is required')
    if action == 'fill' and not needs_value and secret_ref is None and _secret_target(target):
        # поле пароля, токена, кода, карты: значение в шаге не хранится; допустим только параметр с secret: true
        only = _PLACEHOLDER_ONLY.fullmatch(value or '')
        if not (only and params[only.group(1)]['secret']):
            _fail(f'{path}.value', 'secret_required', 'a secret field requires secret_ref')

    precondition = expect = None
    if raw.get('precondition') is not None:
        precondition = _condition(f'{path}.precondition', raw['precondition'], params, ('url_matches', 'visible'))
    if raw.get('expect') is not None:
        expect = _condition(f'{path}.expect', raw['expect'], params, ('url_matches', 'visible', 'text', 'timeout_ms'))

    out = {'id': step_id, 'action': action, 'target': target, 'value': value, 'secret_ref': secret_ref,
           'precondition': precondition, 'expect': expect, 'safe_to_retry': safe, 'risk': 'none'}
    if needs_value:
        out['needs_value'] = True
        if needs_secret:
            out['needs_secret'] = True
    return out, requested


def _secret_input(step: dict, params: dict) -> bool:
    """`fill`, значение которого берётся из хранилища: `secret_ref` или единственная подстановка секретного параметра."""
    if step['action'] != 'fill':
        return False
    if step.get('secret_ref'):
        return True
    only = _PLACEHOLDER_ONLY.fullmatch(step.get('value') or '')
    return bool(only and params.get(only.group(1), {}).get('secret'))


def _secret_steps(steps: list[dict], params: dict) -> None:
    """Секрет вводится только туда, куда процедура сама привела: раньше в ней есть `navigate` или у шага задано
    `precondition.url_matches` (422 `secret_no_context`). Иначе секрет ушёл бы в любую открытую вкладку. Повторять такой шаг
    автоматически нельзя: `safe_to_retry` у него всегда `false`."""
    navigated = False
    for index, step in enumerate(steps):
        if step['action'] == 'navigate':
            navigated = True
        if not _secret_input(step, params):
            continue
        if not navigated and not (step.get('precondition') or {}).get('url_matches'):
            _fail(f'steps[{index}].precondition', 'secret_no_context',
                  'a secret fill needs an earlier navigate or a url_matches precondition')
        step['safe_to_retry'] = False


def normalize_steps(steps, params: list[dict], *, strict_risk: bool = True) -> list[dict]:
    """Шаги в нормальном виде с риском. Риск вычисляет ядро; клиент может только повысить. Ниже вычисленного: 422
    `steps[i].risk: risk_below_computed` при `strict_risk`; импорт и запись из turn передают `strict_risk=False`, там риск
    просто пересчитывается."""
    if not isinstance(steps, list):
        _fail('steps', 'type', 'must be an array')
    if len(steps) > STEPS_MAX:
        _fail('steps', 'too_many', f'at most {STEPS_MAX} steps')
    by_name = {param['name']: param for param in params}
    out, requested, seen = [], [], set()
    for index, raw in enumerate(steps):
        item, asked = _step(index, raw, by_name)
        if item['id'] in seen:
            _fail(f'steps[{index}].id', 'duplicate', 'duplicate id')
        seen.add(item['id'])
        out.append(item)
        requested.append(asked)
    _secret_steps(out, by_name)
    for index, (item, labels, asked) in enumerate(zip(out, _step_labels(out), requested)):
        computed, _ = _split(labels)
        if asked is not None and strict_risk and _rank(asked) < _rank(computed):
            _fail(f'steps[{index}].risk', 'risk_below_computed', 'risk is lower than the one computed by the core')
        risk, flags = _split(labels, asked or 'none')
        item['risk'] = risk
        if flags:
            item['flags'] = flags
    return out


def normalize_procedure(params, steps, *, strict_risk: bool = True) -> tuple[list[dict], list[dict]]:
    params = normalize_params(params)
    return params, normalize_steps(steps, params, strict_risk=strict_risk)


def status_for(steps: list[dict]) -> str:
    """draft, пока в процедуре есть шаг, которому человек не задал значение; иначе active."""
    return 'draft' if any(step.get('needs_value') for step in steps) else 'active'


# --- запуск: подстановка параметров ----------------------------------------------------------------------------

def _plain(path: str, value) -> str:
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value
    _fail(path, 'param_missing', 'param has no value')


def _substitute(path: str, text: str, values: dict) -> str:
    """Один проход: значение параметра вставляется как есть и повторно не разбирается (`{{other}}` в значении остаётся
    текстом)."""
    def one(match):
        name = match.group(1)
        if values.get(name) is None:
            _fail(path, 'param_missing', 'param has no value')
        return _plain(path, values[name])
    return _PLACEHOLDER.sub(one, text)


def _substitute_target(path: str, target: dict | None, values: dict) -> dict | None:
    if not target:
        return target
    return {key: _substitute(f'{path}.{key}', text, values) for key, text in target.items()}


def _substitute_condition(path: str, cond: dict | None, values: dict) -> dict | None:
    if not cond:
        return cond
    out = dict(cond)
    if 'visible' in out:
        out['visible'] = _substitute_target(f'{path}.visible', out['visible'], values)
    if 'text' in out:
        out['text'] = _substitute(f'{path}.text', out['text'], values)
    return out


def resolve_step(step: dict, params: dict, secrets_lookup=None, *, secret_params=frozenset(), path: str = 'step'):
    """Шаг к исполнению: пара `(шаг, значения, которые не журналировать)`.

    `params`: значения запуска по имени (для секретного параметра там ссылка `vault:<имя>`; какие параметры секретные,
    говорит `secret_params`). `secrets_lookup(имя) -> значение | None` достаёт секрет из хранилища. Подстановка в один
    проход. Адрес `navigate` после подстановки проходит `url_forbidden`. Риск пересчитывается по подставленной цели и
    становится max(сохранённый, пересчитанный): параметр «Оплатить» в имени кнопки даёт `pay`. Скрытые значения (секреты,
    одиночный печатный символ в press) не пишутся ни в журнал шагов, ни в события, ни в контекст модели."""
    out = dict(step)
    hidden: set[str] = set()
    action = step['action']

    out['target'] = _substitute_target(f'{path}.target', step.get('target'), params)
    if action == 'navigate':
        if reason := url_forbidden(out['target']['url']):
            _fail(f'{path}.target.url', 'url_forbidden', f'forbidden ({reason})')
    out['precondition'] = _substitute_condition(f'{path}.precondition', step.get('precondition'), params)
    out['expect'] = _substitute_condition(f'{path}.expect', step.get('expect'), params)

    def secret_value(ref) -> str:
        found = None
        match = _SECRET_REF.fullmatch(ref) if isinstance(ref, str) else None
        if match and secrets_lookup is not None:
            found = secrets_lookup(match.group(1))
        if not isinstance(found, str) or not found:
            _fail(f'{path}.value', 'secret_not_found', 'secret was not found')
        hidden.add(found)
        return found

    value, from_secret = step.get('value'), False
    if action == 'fill' and step.get('secret_ref'):
        out['value'], from_secret = secret_value(step['secret_ref']), True
    elif value is not None:
        only = _PLACEHOLDER_ONLY.fullmatch(value) if action == 'fill' else None
        if only and only.group(1) in secret_params:
            out['value'], from_secret = secret_value(params.get(only.group(1))), True
        else:
            out['value'] = _substitute(f'{path}.value', value, params)
    if action == 'wait' and out['value'] is not None and (
            not _DIGITS.fullmatch(out['value']) or int(out['value']) > WAIT_MAX):
        _fail(f'{path}.value', 'invalid', f'wait expects milliseconds up to {WAIT_MAX}')
    if action == 'fill' and not from_secret and _secret_target(out['target']):
        _fail(f'{path}.value', 'secret_required', 'a secret field requires secret_ref')
    if action == 'press' and isinstance(out['value'], str) and len(out['value']) == 1 \
            and out['value'].isprintable() and not out['value'].isspace():
        hidden.add(out['value'])

    recomputed, _ = _split(_own_labels(out))
    out['risk'] = recomputed if _rank(recomputed) > _rank(step.get('risk') or 'none') else step.get('risk') or 'none'
    return out, hidden


def raise_risk(risk: str, flags, label: str) -> tuple[str, list[str]]:
    """`(risk, flags)` с добавленной меткой: строжайшая в риск, остальные жёсткие в flags (как `_split`)."""
    return _split({risk, *(flags or ()), label})


_DEFAULT_PORTS = {'http': 80, 'https': 443}


def page_origin(url) -> str | None:
    """`схема://хост[:порт]` страницы http(s): хост в punycode, порт по умолчанию опущен. Другая схема (about:blank, file),
    пустое и битое: None. Подтверждение привязано к этому значению, исполнитель сверяет его же (`originOf` в
    procedure-step.mjs)."""
    if not isinstance(url, str) or not url:
        return None
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError:
        return None
    if parts.scheme not in _DEFAULT_PORTS or not host:
        return None
    try:
        host = host.encode('idna').decode('ascii')
    except UnicodeError:
        pass
    if ':' in host:
        host = f'[{host}]'
    return f"{parts.scheme}://{host}{f':{port}' if port and port != _DEFAULT_PORTS[parts.scheme] else ''}"


def risk_with_live(step: dict, role, name) -> tuple[str, list[str]]:
    """Риск шага по живой подписи элемента, найденного на странице (роль и имя читает исполнитель, не запись): строжайший из
    сохранённого (`risk`, `flags`) и меток, которые классификатор даёт подписи. Подпись без роли ничего не добавляет:
    селектор, у которого aria-снимок не разобрался. `(risk, flags)` как у `_split`: `none`, если меток нет."""
    labels = {step.get('risk') or 'none', *(step.get('flags') or ())}
    if step.get('action') in ('click', 'fill', 'press', 'select') and isinstance(role, str) and role:
        target = {'role': strip_invisible(role), 'name': strip_invisible(name)}
        labels.update(browser_labels(_classifier_args(step['action'], target)))
        if step['action'] == 'fill' and _secret_target(target):
            labels.add('login')
    return _split(labels)


# --- события browser_step в шаги --------------------------------------------------------------------------------

def events_to_steps(events) -> list[dict]:
    """Черновик шагов из payload событий `browser_step` (в порядке seq). snapshot, screenshot и неуспешные действия
    отбрасываются, подряд идущие navigate на один адрес схлопываются. Значение fill в событии не хранится: шаг получает
    `value: null`, `needs_value: true`; поле пароля, платёжных данных и кода, а также безымянное поле ещё
    `needs_secret: true`. Нет роли и имени элемента (событие записано до их сохранения или ref неизвестен):
    ProcedureError, шаг без цели не воспроизвести."""
    steps: list[dict] = []
    for index, event in enumerate(events):
        if not isinstance(event, dict) or event.get('action') not in ('navigate', 'click', 'fill') \
                or event.get('result') != 'ok':
            continue
        action = event['action']
        if action == 'navigate':
            url = event.get('url')
            if not isinstance(url, str) or not url:
                continue
            if steps and steps[-1]['action'] == 'navigate' and steps[-1]['target']['url'] == url:
                continue
            steps.append({'action': 'navigate', 'target': {'url': url}, 'safe_to_retry': True})
        else:
            role, name = event.get('role'), event.get('name')
            if not isinstance(role, str) or not role or not isinstance(name, str):
                _fail(f'events[{index}].target', 'target_missing', 'element role and name were not recorded')
            target = {'role': role.lower(), 'name': strip_invisible(name)}
            step = {'action': action, 'target': target, 'safe_to_retry': False}
            if action == 'fill':
                step |= {'value': None, 'secret_ref': None, 'needs_value': True}
                if event.get('secret') is True or _secret_target(target) or not target['name'].strip():
                    step['needs_secret'] = True
            steps.append(step)
        if len(steps) > STEPS_MAX:
            _fail('steps', 'too_many', f'at most {STEPS_MAX} steps')
    return [{'id': f's{number}'} | step for number, step in enumerate(steps, 1)]


# --- экспорт и импорт -------------------------------------------------------------------------------------------

def export_document(row) -> dict:
    """Процедура для файла: без owner_id, bot_id, идентификаторов, версии, источника и статуса."""
    return {'format': FORMAT, 'name': row['name'], 'description': row['description'], 'params': row['params'],
            'steps': row['steps']}


def parse_import(doc) -> dict:
    """Форма файла импорта. Содержимое params и steps проверит normalize_procedure."""
    doc = _object('body', doc, ('format', 'name', 'description', 'params', 'steps'))
    if 'format' in doc and doc['format'] != FORMAT:
        _fail('format', 'unsupported', 'unsupported format')
    params = doc.get('params', [])
    steps = doc.get('steps', [])
    if not isinstance(params, list):
        _fail('params', 'type', 'must be an array')
    if not isinstance(steps, list):
        _fail('steps', 'type', 'must be an array')
    return {'name': check_name(doc.get('name')), 'description': check_description(doc.get('description', '')),
            'params': params, 'steps': steps}
