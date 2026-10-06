"""Проверка всего, что приходит от вызывающего. Идентификаторы попадают в имена контейнеров, сетей и
iptables-правил, поэтому шаблоны узкие: первый символ буква или цифра (никаких `-flag`), без точек и слэшей."""
import json
import re
from urllib.parse import unquote

from .errors import ValidationFailed

BOT_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
OWNER_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
EXEC_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
SESSION_ID_RE = re.compile(r"[0-9a-f]{8,64}")
ENV_NAME_RE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
COMMAND_NAME_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}")

ARGV_MAX_ITEMS = 256
ARGV_MAX_BYTES = 128 * 1024
ENV_MAX_ITEMS = 64
ENV_VALUE_MAX = 16 * 1024
STDIN_MAX = 8 * 1024 * 1024
INPUT_MAX = 64 * 1024
PROCEDURE_PAYLOAD_MAX = 256 * 1024
PROCEDURE_TIMEOUT_MIN = 5.0
PROCEDURE_TIMEOUT_MAX = 180.0
PROCEDURE_TIMEOUT_DEFAULT = 30.0
TERM_MAX = 500
BROWSER_MODES = ("bot", "human")
BROWSER_URL_MAX = 2048
# Только http(s) и about:blank, без пробелов, управляющих символов и обратной косой черты: адрес уходит в командную строку
# Chromium и первым символом не может быть `-`.
BROWSER_URL_RE = re.compile(r"https?://[^\s\x00-\x1f\x7f\\]+")
# Невидимые и меняющие направление текста символы: адрес с ними читается иначе, чем он есть. Ядро такие адреса человеку
# не отдаёт; здесь вторая проверка. Тело класса регулярного выражения, символы записаны escape-последовательностями.
# Список один на пакет и совпадает с `HIDDEN_URL_CHARS` в core/bothub/browser_control.py (тест сверяет оба).
HIDDEN_URL_CHARS = (
    "\u00ad\u034f\u061c\u115f\u1160\u17b4\u17b5\u180b-\u180f"  # soft hyphen, grapheme joiner, Arabic letter mark, fillers, Mongolian
    "\u200b-\u200f\u2028\u2029\u202a-\u202e"  # zero-width, marks, line and paragraph separators, bidi embeddings and overrides
    "\u2060-\u2064\u2066-\u206f"  # word joiner, invisible operators, bidi isolates, deprecated format characters
    "\u3164\ufe00-\ufe0f\ufeff\uffa0\ufff9-\ufffb"  # fillers, variation selectors, BOM, annotation characters
    "\U0001d173-\U0001d17a\U000e0000-\U000e0fff"  # musical format characters, tags and variation selectors supplement
)
BROWSER_URL_HIDDEN_RE = re.compile(f"[{HIDDEN_URL_CHARS}]")

# Загрузчик, оболочка и клиент docker: эти переменные меняют поведение процесса или самого docker exec.
ENV_DENY_EXACT = {"PATH", "HOME", "USER", "SHELL", "IFS", "ENV", "BASH_ENV", "PS4", "PROMPT_COMMAND", "TMPDIR"}
ENV_DENY_PREFIX = ("LD_", "DYLD_", "DOCKER_", "BASH_FUNC_")


def _match(pattern: re.Pattern, value, what: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValidationFailed(f"{what}: недопустимое значение")
    return value


def validate_bot_id(value) -> str:
    return _match(BOT_ID_RE, value, "bot_id")


def validate_owner_id(value) -> str:
    return _match(OWNER_ID_RE, value, "owner_id")


def validate_exec_id(value) -> str:
    return _match(EXEC_ID_RE, value, "exec_id")


def validate_session_id(value) -> str:
    return _match(SESSION_ID_RE, value, "session_id")


def validate_command_name(value) -> str:
    return _match(COMMAND_NAME_RE, value, "command")


def _no_nul(value: str, what: str) -> None:
    if "\x00" in value:
        raise ValidationFailed(f"{what}: нулевой байт")


def validate_argv(argv) -> list[str]:
    if not isinstance(argv, list) or not argv:
        raise ValidationFailed("argv: нужен непустой список строк")
    if len(argv) > ARGV_MAX_ITEMS:
        raise ValidationFailed("argv: слишком много элементов")
    size = 0
    for item in argv:
        if not isinstance(item, str):
            raise ValidationFailed("argv: элементы должны быть строками")
        _no_nul(item, "argv")
        size += len(item.encode()) + 1
    if size > ARGV_MAX_BYTES:
        raise ValidationFailed("argv: слишком длинная команда")
    if not argv[0] or argv[0].startswith("-"):
        raise ValidationFailed("argv: первый элемент должен быть именем программы")
    return list(argv)


def validate_env(env) -> dict[str, str]:
    if env is None:
        return {}
    if not isinstance(env, dict) or len(env) > ENV_MAX_ITEMS:
        raise ValidationFailed("env: нужен словарь не длиннее 64 пар")
    for name, value in env.items():
        if not isinstance(name, str) or not ENV_NAME_RE.fullmatch(name):
            raise ValidationFailed("env: имя переменной должно быть в формате ABC_123")
        if name in ENV_DENY_EXACT or name.startswith(ENV_DENY_PREFIX):
            raise ValidationFailed(f"env: переменная {name} запрещена")
        if not isinstance(value, str) or len(value) > ENV_VALUE_MAX:
            raise ValidationFailed(f"env: значение {name} должно быть строкой до 16 КиБ")
        _no_nul(value, "env")
    return dict(env)


def validate_stdin(value) -> bytes:
    if value is None:
        return b""
    if not isinstance(value, str):
        raise ValidationFailed("stdin: нужна строка")
    data = value.encode()
    if len(data) > STDIN_MAX:
        raise ValidationFailed("stdin: больше 8 МиБ")
    return data


def validate_term_size(cols, rows) -> tuple[int, int]:
    for v in (cols, rows):
        if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= TERM_MAX:
            raise ValidationFailed("размер терминала: целые числа от 1 до 500")
    return cols, rows


def validate_input(data: bytes) -> bytes:
    if len(data) > INPUT_MAX:
        raise ValidationFailed("ввод: больше 64 КиБ за раз")
    return data


def validate_browser_mode(value) -> str:
    if not isinstance(value, str) or value not in BROWSER_MODES:
        raise ValidationFailed("mode: bot или human")
    return value


def validate_browser_url(value) -> str:
    """Адрес, который человек увидит в чистом Chromium. Нет адреса: about:blank."""
    if value is None:
        return "about:blank"
    if not isinstance(value, str) or len(value) > BROWSER_URL_MAX:
        raise ValidationFailed("url: строка до 2048 символов")
    if value != "about:blank" and not BROWSER_URL_RE.fullmatch(value):
        raise ValidationFailed("url: только http, https или about:blank без пробелов и управляющих символов")
    if BROWSER_URL_HIDDEN_RE.search(value):
        raise ValidationFailed("url: невидимые и меняющие направление текста символы запрещены")
    if value != "about:blank":
        # Chromium раскодирует %-запись имени хоста: невидимый символ в ней читается так же, как открытый.
        netloc = value.split("/", 3)[2]
        if "%" in netloc and BROWSER_URL_HIDDEN_RE.search(unquote(netloc, errors="replace")):
            raise ValidationFailed("url: невидимые и меняющие направление текста символы запрещены")
    return value


def validate_procedure_payload(value) -> dict:
    """Шаг процедуры одной JSON-строкой (объект). Содержимое лаунчер не разбирает и нигде не пишет: там бывают значения
    секретов. Текст ошибки не повторяет ни строку, ни её куски."""
    if not isinstance(value, str):
        raise ValidationFailed("payload_json: нужна строка")
    if len(value.encode("utf-8", "surrogatepass")) > PROCEDURE_PAYLOAD_MAX:
        raise ValidationFailed("payload_json: больше 256 КиБ")
    try:
        parsed = json.loads(value)
    except (ValueError, RecursionError):
        raise ValidationFailed("payload_json: не JSON") from None
    if not isinstance(parsed, dict):
        raise ValidationFailed("payload_json: нужен объект")
    return parsed


def validate_procedure_timeout(value) -> float:
    if value is None:
        return PROCEDURE_TIMEOUT_DEFAULT
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or not PROCEDURE_TIMEOUT_MIN <= value <= PROCEDURE_TIMEOUT_MAX:
        raise ValidationFailed(f"timeout: от {PROCEDURE_TIMEOUT_MIN:g} до {PROCEDURE_TIMEOUT_MAX:g} секунд")
    return float(value)
