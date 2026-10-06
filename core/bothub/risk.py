"""Классификация риска и политика авто-разрешений. Контракт: docs/contracts.md, раздел 4.

`classify(tool_name, args)` -> "pay"|"send"|"delete"|"login"|"push"|"exec"|"other"
(метки ограничены CHECK в миграциях 001/002; установка ПО и отправка данных наружу
из Bash идут под метками "exec" и "send").

`decide(bot, tool, args)` - единственное место, где решается авто-разрешение
(`bothub.main.decide_permission` - тонкая обёртка). Принципы:
- fail-closed: авто-разрешается только то, что классификатор признал безопасным;
  непонятный инструмент, аргументы или команда (обфускация, `$(...)`, base64) требуют
  подтверждения владельца;
- pay/delete/login, установка ПО, отправка данных наружу из Bash и любые не читающие
  действия на Mac никогда не проходят ни по auto_allow, ни по "запомнить";
- аргументы в правилах сравниваются точно, без glob (glob только в имени инструмента, и
  для неизвестных инструментов он ничего не разрешает: нужно точное имя и непустой match);
- браузер бота (`mcp__bothub__browser`): snapshot и screenshot без вопроса; navigate только на
  http/https/about:blank и не во внутренние адреса (иначе отказ без подтверждения, `forbidden_reason`);
  fill в похожее на секретное поле и click на оплату, удаление, вход всегда спрашивают; прочее
  спрашивает и запоминается по паре (действие, origin).

ponytail: проверка команды - allowlist читающих программ плюс regex-запреты, не полная
грамматика shell. Всё, что не распознано как чтение, уходит на подтверждение.
"""
import hashlib
import json
import posixpath
import re
import shlex
import unicodedata
from fnmatch import fnmatchcase

from .browser_control import is_browser_tool, url_forbidden, url_origin

_MAC_MOVE_TO_TRASH = "mac_move_to_trash"
_COMMAND_KEYS = ("command", "cmd", "script")
_URL_KEYS = ("url",)

_GIT_PUSH_RE = re.compile(r"\bgit\s+push\b")

_LOGIN_WORDS = ("login", "auth", "password", "credential", "signin", "sign-in")
_PAY_WORDS = ("pay", "purchase", "checkout", "invoice", "charge", "buy")
_SEND_WORDS = ("submit", "send")
_PUSH_WORDS = ("push",)
_DELETE_WORDS = ("delete", "trash", "remove")

HARD_RISKS = frozenset({"pay", "delete", "login"})

# Раздел 5/8 контракта: инструменты Mac, которые всегда требуют approved-подтверждения
# (риск не 'other' по смыслу продукта, независимо от того, что вернёт classify() на
# пустых args), против чисто читающих. Владелец: main.py (/api/mac/call) и builder.py
# (конструктор ботов не даёт положить их в auto_allow) - оба берут список отсюда, чтобы
# не разойтись.
MAC_RISKY_TOOLS = ("shell", "applescript", "shortcut", "open", "click", "type_text", "move_to_trash", "delegate")
MAC_READ_TOOLS = ("find_files", "read_file", "preview", "screenshot", "upload_file")
_MAC_SAFE_TOOLS = MAC_READ_TOOLS

# Инструменты бота, которые не меняют ничего снаружи. Имена строго как у Claude CLI
# (регистр важен) и у MCP-сервера bothub.
READ_ONLY_TOOLS = frozenset({
    "Read", "Glob", "Grep", "TodoWrite", "WebSearch",
    "mcp__bothub__attach_file", "mcp__bothub__remember",
    # пробуждение только ставит будущий ход этому же боту (до 20 штук, до 30 дней, видно в ленте и отменяется владельцем)
    "mcp__bothub__schedule_wakeup",
    # результат поручения читается только отправителем, ядро проверяет это по токену бота (раздел 20). Сама постановка
    # поручения (`mcp__bothub__delegate_to_bot`) сюда не входит: она идёт по одобрению владельца или точному правилу auto_allow
    "mcp__bothub__delegation_result",
})
# Write/Edit авто-разрешаются только внутри домашнего каталога бота (см. _home_path_ok).
HOME_WRITE_TOOLS = frozenset({"Write", "Edit"})
BOT_HOME = "/home/bot"
# Инструкционные файлы и каталоги: их подхватывают CLI, make, pytest, npm, python и shell, поэтому
# правка равна выполнению кода или смене политики. Имена сравниваются без учёта регистра.
# `.local/bin` покрыт скрытым компонентом пути и каталогом `bin`.
_INSTRUCTION_FILES = frozenset({
    "claude.md", "claude.local.md", "agents.md", "gemini.md", "makefile", "gnumakefile", "package.json",
    "conftest.py", "pytest.ini", "pyproject.toml", "setup.py", "setup.cfg", "sitecustomize.py", "usercustomize.py",
})
_INSTRUCTION_DIRS = frozenset({"node_modules", "bin"})
# Пути с учётными данными: Read, attach_file и аргументы читающих команд контейнера без подтверждения
# туда не ходят. Имена каталогов ищутся в любом месте пути, без учёта регистра.
_CRED_DIRS = frozenset({".claude", ".codex", ".gemini", ".auth", ".ssh", ".aws", ".gnupg"})
_CRED_SPECIAL_RE = re.compile(r"^/proc/.+/environ$|(?:^|/)run/secrets(?:/|$)")
# Инструменты с путём в аргументе: (ключ, обязателен ли).
_PATH_ARG_TOOLS = {"Read": ("file_path", True), "mcp__bothub__attach_file": ("path", True), "Grep": ("path", False)}

_MAC_TOOL_RE = re.compile(r"(?:^|__)mac_([a-z_]+)$")

# Повышение прав и учётки: в позиции команды (в том числе после обёртки env/command/...),
# sudo и doas ещё и где угодно как отдельное слово. `cat /etc/passwd` сюда не попадает.
_SUDO_RE = re.compile(
    r"(?:^|[;&|(]\s*|\b(?:xargs|env|command|exec|nohup|time|nice|builtin|then|do)\s+)"
    r"(?:sudo|doas|su|passwd|chpasswd|visudo|ssh-keygen|security|dscl|login)(?![\w-])"
    r"|(?<![\w./-])(?:sudo|doas)(?![\w-])|keychain"
)
_MAC_SEND_RE = re.compile(r"\b(curl|wget|ssh|scp|sftp|rsync|nc|ncat|netcat|telnet|ftp|mail|sendmail|osascript)\b|https?://")
_MAC_DELETE_RE = re.compile(
    r"(?:^|[;&|]\s*|\s)(?:/\S*/)?(rm|rmdir|shred|srm|dd|truncate|diskutil|mk" r"fs)\b|\bgit\s+(reset|clean)\b|empty trash"
)
_NET_RE = re.compile(
    r"(?<![\w.-])(?:curl|wget|dig|nc|ncat|netcat|socat|scp|sftp|rsync|ssh|ftp|tftp|telnet|sendmail|aria2c)(?![\w-])"
)
_PIP_INDEX_RE = re.compile(r"\b(?:pip3?|pipx|uv)\b[^;&|\n]*\s--(?:extra-)?index-url(?:\s|=)")
_WRITE_EXEC_RE = re.compile(r"\b(?:sed\s+(?:-\S+\s+)*-i\b|find\b[^;&|\n]*\s-(?:exec|execdir|ok|okdir|delete)\b|awk\b[^;&|\n]*\bsystem\s*\(|docker\b[^;&|\n]*\s(?:-v|--volume)(?:\s|=)/)")
_INSTALL_RE = re.compile(
    r"(?<![\w.-])(?:"
    r"(?:apt|apt-get|aptitude|dnf|yum|apk|snap|brew|pacman|zypper|port)\s+(?:-\S+\s+)*"
    r"(?:install|reinstall|upgrade|dist-upgrade|add|update)\b"
    r"|dpkg\s+(?:-\S+\s+)*(?:-i|--install|-r|--remove|-p|--purge)\b"
    r"|(?:pip3?|pipx|poetry|conda|mamba|uv(?:\s+pip|\s+tool)?)\s+(?:-\S+\s+)*(?:install|add|sync)\b"
    r"|(?:npm|yarn|pnpm|bun)\s+(?:-\S+\s+)*(?:install|i|add|ci|link|update|up|upgrade|dlx|x)\b"
    r"|npx\b"
    r"|(?:gem|cargo|go|composer)\s+(?:-\S+\s+)*(?:install|add|get)\b"
    r")"
)
# Запуск произвольного кода: интерпретатор с -c/-e, конвейер в shell.
_INLINE_RE = re.compile(
    r"(?<![\w.-])(?:python[\d.]*|perl|ruby|node|php|lua|bash|sh|zsh|dash|ksh)\s+(?:-\S*\s+)*-[a-z]*[ce]\b"
    r"|\|\s*(?:sudo\s+)?(?:sh|bash|zsh|dash|python[\d.]*|perl|ruby|node)\b"
)
# Обфускация: подстановки, склейка кавычками, base64, heredoc, обратный слеш, brace expansion.
_OBFUSCATION_RE = re.compile(
    r"\$[({'\"\w@*#?!$-]|`|<\(|>\(|<<|\\|[\"']{2}"
    r"|(?<![\w.-])(?:eval|exec|xargs|base64|basenc|xxd|uudecode|openssl)(?![\w-])"
    r"|/dev/(?:tcp|udp)/|[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]"
    r"|\{[^{}\s]*[,.][^{}\s]*\}"
)
_SAFE_REDIRECT_RE = re.compile(r"\s(?:[12]?>>?\s*/dev/null|&>>?\s*/dev/null|[12]>&[12])(?=\s|$)")
_SEPARATORS = frozenset({"|", "||", "&&", ";"})
_PUNCT = frozenset("();<>|&")
_GLOB_CHARS = frozenset("*?[")
# Шаг процедуры (approvals.tool, bothub/procedure_runner.py): подтверждается на каждом запуске, «запомнить» его не снимает.
PROCEDURE_TOOL = "procedure_step"

# Читающие программы: без записи, сети и запуска других программ. Вызов только по имени
# (путь с `/` не признаётся), аргументы дополнительно проверяет _read_args_ok.
_READ_COMMANDS = frozenset(
    "ls cat head tail wc pwd echo grep egrep fgrep find mdfind mdls stat file du df date whoami id "
    "uname uptime sw_vers which sort cut tr nl rev basename dirname realpath tree".split()
)
# Контейнер: одиночные команды без перенаправлений, пайпов и подстановок. git сюда не входит:
# через конфиги и хуки он запускает код, поэтому любая git-команда идёт на подтверждение.
_CONTAINER_READ_COMMANDS = frozenset(
    "ls cat grep pwd head tail wc find du sort uniq which echo stat file tree diff".split()
)
_FIND_PATTERN_TESTS = frozenset({"-name", "-iname", "-path", "-ipath", "-wholename", "-iwholename", "-regex", "-iregex", "-lname", "-ilname"})
_SECRET_PATH_RE = re.compile(r"(?:^|[/.])(?:\.ssh|\.aws|\.gnupg|\.env(?:\.[^/ ]*)?|\.claude|\.git/config|credentials?|secrets?|id_rsa|id_ed25519)(?:/|\b)", re.I)
_FIND_WRITE_ARGS = frozenset({"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0", "-fprintf", "-fls"})

# Браузер бота. Поля вызова: action, target, element (описание), page_label (подпись элемента со страницы,
# ставит mcp_server по ref из snapshot), value, url, origin (origin текущей страницы, ставит mcp_server).
_BROWSER_READ_ACTIONS = frozenset({"snapshot", "screenshot"})
_BROWSER_ACTIONS = _BROWSER_READ_ACTIONS | {"navigate", "click", "fill"}
_BROWSER_KEYS = frozenset({"action", "target", "element", "page_label", "value", "url", "origin"})
_BARE_REF_RE = re.compile(r"[a-z]{0,2}\d+(?:[a-z]\d+)*", re.I)  # ref из snapshot: e12, f2e5


def _words(*stems: str) -> re.Pattern:
    return re.compile(r"(?<!\w)(?:" + "|".join(stems) + r")(?!\w)", re.I)


# Метка по тексту элемента (поле для fill, кнопка для click). Порядок проверки: login, delete, pay.
_BROWSER_LOGIN_RE = _words(
    r"pass(?:word|code|phrase)?", r"pwd", r"secret", r"token", r"otp", r"totp", r"2fa", r"mfa", r"pin",
    r"api[ _-]?key", r"one[- ]time", r"(?:verification|security|confirmation|sms) code", r"ssn",
    r"social security", r"passport", r"log[ _-]?in", r"sign[ _-]?in", r"authori[sz]e", r"(?:allow|grant) access",
    r"парол\w*", r"секрет\w*", r"токен\w*", r"одноразов\w*", r"пин[- ]?код", r"код (?:подтверждения|из смс)",
    r"смс[- ]?код", r"паспорт\w*", r"снилс", r"войти", r"вход", r"авторизу\w*", r"авторизова\w*",
    r"разрешить доступ")
_BROWSER_DELETE_RE = _words(
    r"delet\w*", r"remov\w*", r"trash", r"eras\w*", r"destroy", r"clear (?:all|history|data)",
    r"close (?:my )?account", r"deactivat\w*", r"удал\w*", r"убрать", r"стереть", r"очистить",
    r"закрыть аккаунт", r"деактивир\w*")
_BROWSER_PAY_RE = _words(
    # Основы слов не берём целиком: pay\w* ловил "Payload size", order\w* "Sort by order date", карт\w* "Картинки"
    # и "Яндекс Карты". Заказ оформляет кнопка с глаголом, поэтому order только в таких оборотах.
    r"pay(?:ments?|ing|out|pal|s)?", r"purchas\w*", r"check[- ]?out", r"buy\w*", r"donat\w*", r"subscrib\w*",
    r"(?:place|confirm|complete|submit|finali[sz]e)(?: (?:an|your|the|my))? order", r"order (?:now|and pay|& pay)",
    r"credit card", r"debit card", r"card (?:number|holder)", r"cvv", r"cvc", r"iban", r"expir\w*",
    r"routing number", r"account number", r"invoice", r"charge", r"оплат\w*", r"купить", r"покупк\w*",
    r"заказ\w*", r"оформить", r"оформление", r"перевести", r"пожертвова\w*", r"подписк\w*",
    r"банковск\w* карт\w*", r"карт(?:ой|е|у) (?:visa|mastercard|мир)", r"по карте", r"картой",
    r"номер карты", r"данные карты", r"держатель карты", r"владелец карты", r"срок действия",
    # Карта и деньги по глаголу: голое "карта"/"перевод" ловило бы "Яндекс Карты", "Перевод текста", "Карточка товара".
    r"(?:add|save|link|attach|bind|store|update|change|enter)(?: (?:a|your|my|the|new|payment|bank|credit|debit))* cards?",
    r"(?:привязать|добавить|сохранить|привязка|добавление|сохранение|указать|ввести|изменить|обновить)"
    r"(?: (?:новую|свою|мою|банковскую|платёжную|платежную))* карт(?:а|у|ы|е|ой)",
    r"плат[её]ж\w*", r"пополни\w* (?:баланс|счёт|счет|кошел[её]к|карту)", r"пополнение",
    r"перевод(?:ы)? (?:средств|денег|денежных|на карту|на счёт|на счет)", r"вывести (?:средства|деньги)", r"вывод средств",
    r"transfer(?![-\w])", r"(?:wire|bank|money) transfer", r"top[- ]?up", r"withdraw\w*", r"subscriptions?")
_BROWSER_SEND_RE = _words(r"submit", r"send", r"отправить", r"отправк\w*")


# Омоглифы: кириллица и греческий, похожие на латиницу, и обратно. Подпись «Pаy» (кириллическая «а») человек читает как pay,
# а регулярные выражения классификатора её не видят. Проверка идёт по самому тексту и по двум свёрткам: всё похожее в латиницу
# и всё похожее в кириллицу. Лишние совпадения безопасны: шаг просто попросит подтверждение.
_CYR_TO_LAT = str.maketrans({
    "а": "a", "в": "b", "е": "e", "ё": "e", "к": "k", "м": "m", "н": "h", "о": "o", "р": "p", "с": "c", "т": "t",
    "у": "y", "х": "x", "і": "i", "ї": "i", "ј": "j", "ѕ": "s", "ԁ": "d", "һ": "h",
    "α": "a", "β": "b", "ε": "e", "ι": "i", "κ": "k", "ν": "v", "ο": "o", "ρ": "p", "τ": "t", "υ": "u", "χ": "x"})
_LAT_TO_CYR = str.maketrans({
    "a": "а", "b": "в", "e": "е", "k": "к", "m": "м", "h": "н", "o": "о", "p": "р", "c": "с", "t": "т", "y": "у", "x": "х"})


def homoglyph_variants(text) -> tuple[str, ...]:
    """Текст и его свёртки омоглифов (NFKC, нижний регистр): `(как есть, в латиницу, в кириллицу)` без повторов."""
    text = str(text or "")
    folded = unicodedata.normalize("NFKC", text).casefold()
    return tuple(dict.fromkeys((text, folded.translate(_CYR_TO_LAT), folded.translate(_LAT_TO_CYR))))


def _found(pattern: re.Pattern, text: str) -> bool:
    return any(pattern.search(variant) for variant in homoglyph_variants(text))


def _browser_parts(args: dict) -> list[str]:
    return [value.strip() for key in ("target", "element", "page_label")
            if isinstance(value := args.get(key), str) and value.strip()]


def _browser_described(args: dict) -> bool:
    """Есть ли у click/fill описание, по которому можно судить об элементе (а не одни refs)."""
    return any(not _BARE_REF_RE.fullmatch(part) for part in _browser_parts(args))


def _analyze_browser(args: dict) -> tuple[str, bool]:
    action = args.get("action")
    if action not in _BROWSER_ACTIONS or set(args) - _BROWSER_KEYS:
        return "exec", True
    if action in _BROWSER_READ_ACTIONS:
        return "other", False
    if action == "navigate":
        return ("other", False) if url_forbidden(args.get("url")) is None else ("exec", True)
    text = " ".join(_browser_parts(args))
    for label, pattern in (("login", _BROWSER_LOGIN_RE), ("delete", _BROWSER_DELETE_RE), ("pay", _BROWSER_PAY_RE)):
        if _found(pattern, text):
            return label, True
    if action == "click" and _found(_BROWSER_SEND_RE, text):
        return "send", False
    return "other", False


def browser_labels(args: dict) -> list[str]:
    """Все метки, под которые подходит описание click/fill (login, delete, pay, у click ещё send), без отсева по порядку:
    `_analyze_browser` отдаёт первую. Нужна процедурам: шаг, которому подходят две метки, хранит строгую и помнит вторую."""
    if args.get("action") not in ("click", "fill"):
        return []
    text = " ".join(_browser_parts(args))
    found = [label for label, pattern in (("login", _BROWSER_LOGIN_RE), ("delete", _BROWSER_DELETE_RE), ("pay", _BROWSER_PAY_RE))
             if _found(pattern, text)]
    if args["action"] == "click" and _found(_BROWSER_SEND_RE, text):
        found.append("send")
    return found


def forbidden_reason(tool_name, args) -> str | None:
    """Код причины, если действие отклоняется сразу, без вопроса владельцу: navigate на адрес вне
    политики (`browser_control.url_forbidden`). Одобрение владельца такой адрес не открывает."""
    if is_browser_tool(tool_name) and isinstance(args, dict) and args.get("action") == "navigate":
        return url_forbidden(args.get("url"))
    return None


def browser_rule_key(args) -> tuple[str, str] | None:
    """(действие, origin), по которым запоминается разрешение. None: запомнить нельзя
    (адрес вне политики, origin страницы неизвестен, элемент без описания, чтение)."""
    if not isinstance(args, dict):
        return None
    action = args.get("action")
    if action == "navigate":
        origin = url_origin(args.get("url"))
    elif action in ("click", "fill") and _browser_described(args):
        page = args.get("origin")
        origin = url_origin(page) if isinstance(page, str) and url_origin(page) == page else None
    else:
        return None
    return (action, origin) if origin else None


def _browser_rule_matches(rule: dict, tool_name: str, args) -> bool:
    match = rule.get("match")
    if rule.get("tool") != tool_name or not is_browser_tool(tool_name) or not isinstance(match, dict) \
            or set(match) != {"action", "origin"} or not isinstance(args, dict):
        return False
    return browser_rule_key(args) == (match["action"], match["origin"])


def _arg_text(value) -> str:
    return value if isinstance(value, str) else json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def op_hash(tool_name, args) -> str:
    """SHA256(tool + канонический JSON args): хэш операции целиком. Одобрение и сохранённое
    правило с таким хэшем действуют только на ту же операцию: тот же инструмент и ровно те
    же аргументы (лишний ключ или другое значение дают другой хэш)."""
    canon = json.dumps(args, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(f"{tool_name}{canon}".encode()).hexdigest()


def _mac_tool(name: str) -> str | None:
    match = _MAC_TOOL_RE.search(name)
    return match.group(1) if match else None


def _parse_command(text: str) -> list[list[str]] | None:
    """Команда -> сегменты (по | || && ;). None, если разобрать нельзя или есть обфускация."""
    if _OBFUSCATION_RE.search(text.lower()):
        return None
    # перевод строки - тот же разделитель команд, shlex считал бы его пробелом
    padded = " " + text.replace("\r", "\n").replace("\n", " ; ") + " "
    padded = _SAFE_REDIRECT_RE.sub(" ", padded)
    lexer = shlex.shlex(padded, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return None
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token in _SEPARATORS:
            segments.append([])
        else:
            segments[-1].append(token)
    return segments


def _read_args_ok(program: str, args: list[str]) -> bool:
    if program == "find":
        return not _FIND_WRITE_ARGS.intersection(args)
    if program in ("sort", "tree"):
        return not any(a.startswith("--output") or a.startswith("--compress-program") or a.startswith("--temporary-directory")
                       or (a[:2] in ("-o", "-T") and not a.startswith("--")) for a in args)
    if program == "date":
        return not any(a.startswith("--set") or (a.startswith("-s") and not a.startswith("--")) for a in args)
    if program == "file":
        return not any(a == "--compile" or (a.startswith("-") and not a.startswith("--") and "C" in a[1:]) for a in args)
    if program == "uniq":
        # второй позиционный аргумент uniq - файл, в который он пишет
        return sum(1 for a in args if not a.startswith("-") or a == "-") <= 1
    return True


def _normalize_path(path: str) -> str | None:
    """Абсолютный нормализованный путь. Относительные пути и `~` считаются от /home/bot.
    None: нормализовать нельзя (пустой путь, NUL, `~user`)."""
    if not path or "\x00" in path:
        return None
    if path == "~" or path.startswith("~/"):
        path = BOT_HOME + path[1:]
    elif path.startswith("~"):
        return None
    elif not path.startswith("/"):
        path = BOT_HOME + "/" + path
    return posixpath.normpath(re.sub(r"/{2,}", "/", path))


def _credential_path(path, allow_glob: bool = False) -> bool:
    """True, если путь указывает на учётные данные или не поддаётся проверке (не строка, glob)."""
    if not isinstance(path, str):
        return True
    if not allow_glob and _GLOB_CHARS.intersection(path):
        return True
    normalized = _normalize_path(path)
    if normalized is None:
        return True
    low = normalized.lower()
    parts = [part for part in low.split("/") if part]
    if any(part in _CRED_DIRS or part.startswith(tuple(d + "." for d in _CRED_DIRS)) for part in parts):
        return True  # в том числе `.claude.json` и `.ssh.bak`
    last = parts[-1] if parts else ""
    return last == ".netrc" or last.startswith(".env") or bool(_CRED_SPECIAL_RE.search(low))


def _container_args_ok(program: str, args: list[str]) -> bool:
    """Аргументы читающей команды контейнера: без записи, без путей к кредам, без glob вместо пути.
    Glob разрешён только там, где это шаблон поиска (find -name, первый операнд grep), а не путь."""
    if not _read_args_ok(program, args):
        return False
    if program == "tail" and any(
            a in ("--follow", "-F") or a.startswith("--follow=")
            or (a.startswith("-") and not a.startswith("--") and "f" in a.lower()[1:]) for a in args):
        return False  # tail -f не завершается
    patterns: set[int] = set()
    if program == "grep":
        patterns = {i for i, a in enumerate(args) if i and args[i - 1] in ("-e", "--regexp")}
        pattern_from_option = bool(patterns) or any(
            a in ("-f", "--file") or a.startswith(("--file=", "--regexp=")) for a in args)
        if not pattern_from_option:
            first = next((i for i, a in enumerate(args) if not a.startswith("-") or a == "-"), None)
            if first is not None:
                patterns.add(first)
    for index, arg in enumerate(args):
        if arg.startswith("-") and arg != "-":
            if "=" in arg and _credential_path(arg.split("=", 1)[1], allow_glob=True):
                return False
            continue
        if index in patterns:
            continue
        if program == "find" and index and args[index - 1] in _FIND_PATTERN_TESTS:
            continue
        if _credential_path(arg):
            return False
    return True


def _read_only_command(text: str) -> bool:
    segments = _parse_command(text)
    if not segments:
        return False
    for segment in segments:
        if not segment or segment[0] not in _READ_COMMANDS:
            return False
        if any(token and _PUNCT.issuperset(token) for token in segment):
            return False  # остаточные >, <, &, ( - запись, фон или подоболочка
        if not _read_args_ok(segment[0], segment[1:]):
            return False
    return True


def _container_read_only(text: str) -> bool:
    segments = _parse_command(text)
    if not segments or len(segments) != 1:
        return False
    segment = segments[0]
    if not segment or segment[0] not in _CONTAINER_READ_COMMANDS:
        return False
    if any(token and _PUNCT.issuperset(token) for token in segment):
        return False
    return not _SECRET_PATH_RE.search(text) and _container_args_ok(segment[0], segment[1:])


def _shell_read_only(args: dict) -> bool:
    primary = args.get("cmd", args.get("command"))
    if not isinstance(primary, str) or not primary.strip():
        return False
    if set(args) - {"cmd", "command", "cwd"}:
        return False
    cwd = args.get("cwd")
    if cwd is not None and (not isinstance(cwd, str) or not cwd.startswith("/")
                            or cwd.startswith("//") or "\x00" in cwd
                            or _SECRET_PATH_RE.search(cwd)
                            or any(part in ("..", ".") or part.startswith(".") for part in cwd.split("/") if part)):
        return False
    values = [args[k] for k in ("cmd", "command") if k in args]
    return all(isinstance(v, str) and _read_only_command(v)
               and not _SECRET_PATH_RE.search(v) for v in values)


def _shell_extra(command: str) -> tuple[str, bool]:
    """Опасное в тексте shell-команды: (метка, blocked). blocked = никогда не авто."""
    low = command.lower()
    if _SUDO_RE.search(low):
        return "login", True
    if _PIP_INDEX_RE.search(low) or _NET_RE.search(low):
        return "send", True
    if _INSTALL_RE.search(low):
        return "exec", True
    if _WRITE_EXEC_RE.search(low):
        return "exec", True
    if _INLINE_RE.search(low):
        return "exec", True
    segments = _parse_command(command)
    if segments is None:
        return "exec", True
    for segment in segments:
        if segment and (_GLOB_CHARS | {"{", "}", "~"}).intersection(segment[0]):
            return "exec", True  # glob или раскрытие в позиции программы
    return "other", False


def _generic_label(name: str, command: str, url: str) -> str:
    if command:
        if _MAC_DELETE_RE.search(command):
            return "delete"
        if _GIT_PUSH_RE.search(command):
            return "push"
    # Раздел 4 контракта: слова ищутся в имени инструмента и в command/cmd/script/url,
    # не во всех args — иначе безобидный текстовый аргумент ("оставь заметку про delete
    # тикет") задирал бы риск выше того, что инструмент реально делает.
    haystack = " ".join([name, command, url])
    def has(words):
        return any(re.search(r"(?<![a-z0-9])" + re.escape(word) + r"(?![a-z0-9])", haystack) for word in words)
    if has(_LOGIN_WORDS):
        return "login"
    if has(_DELETE_WORDS):
        return "delete"
    if has(_PAY_WORDS):
        return "pay"
    if has(_PUSH_WORDS):
        return "push"
    if has(_SEND_WORDS):
        return "send"
    return "other"


def _analyze_mac_control(mac: str, args: dict) -> tuple[str, bool]:
    """Mac-инструменты не из белого списка чтения. Авто может пройти только shell-команда,
    целиком признанная читающей; click/type_text/open/applescript/shortcut и неизвестные
    mac_* - всегда подтверждение."""
    if mac == "move_to_trash":
        return "delete", True
    text = " ".join(v for v in args.values() if isinstance(v, str)).lower()
    if _SUDO_RE.search(text):
        return "login", True
    if _MAC_DELETE_RE.search(text):
        return "delete", True
    if _MAC_SEND_RE.search(text):
        return "send", True
    if mac == "shell" and _shell_read_only(args):
        return "other", False
    return "exec", True


def _analyze(tool_name, args) -> tuple[str, bool]:
    """(метка риска, blocked). blocked: никакое правило и никакое "запомнить" не разрешают."""
    name = tool_name.lower() if isinstance(tool_name, str) else ""
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return "exec", True
    if is_browser_tool(tool_name):
        return _analyze_browser(args)

    mac = _mac_tool(name)
    if mac == "delegate":
        return "exec", True
    if mac and mac not in _MAC_SAFE_TOOLS:
        return _analyze_mac_control(mac, args)

    command = ""
    command_given = False
    for key in _COMMAND_KEYS:
        value = args.get(key)
        if isinstance(value, str):
            command, command_given = value, True
            break
    url = ""
    for key in _URL_KEYS:
        value = args.get(key)
        if isinstance(value, str):
            url = value.lower()
            break

    label = _generic_label(name, command.lower(), url)
    blocked = label in HARD_RISKS

    if name == "bash" and not command_given:
        return "exec", True  # Bash без строки команды: классифицировать нечего
    if name == "bash" and any(key in args and args[key] != command for key in _COMMAND_KEYS):
        return "exec", True  # другой или нестроковый источник команды не должен обходить проверку
    if name == "bash" and _container_read_only(command):
        # читающая команда из белого списка: слова в аргументах (cat pay.txt, grep -r auth .) не дают метку
        label = _generic_label(name, "", url)
        return label, label in HARD_RISKS
    if command_given:
        extra_label, extra_blocked = _shell_extra(command)
        if extra_blocked:
            blocked = True
            if label == "other":
                label = extra_label
    if name == "bash" and (not command_given or not _container_read_only(command)):
        blocked = True
        if label == "other":
            label = "exec"
    return label, blocked


def classify(tool_name: str, args: dict | None = None) -> str:
    return _analyze(tool_name, args)[0]


def _home_path_ok(args: dict) -> bool:
    """Write/Edit: абсолютный путь внутри /home/bot, без `..` наружу и без скрытых
    компонентов (.bashrc, .claude/settings.json, .auth: через них бот получил бы выполнение
    кода или секреты). Симлинки внутри контейнера ядро не видит: их создаёт только Bash,
    а он всегда через подтверждение."""
    path = args.get("file_path")
    if not isinstance(path, str) or not path.startswith("/") or path.startswith("//") or "\x00" in path:
        return False
    if any(part.startswith(".") for part in path.split("/") if part):
        return False
    normalized = posixpath.normpath(path)
    prefix = BOT_HOME + "/"
    if not normalized.startswith(prefix):
        return False
    parts = normalized[len(prefix):].split("/")
    if any(part.startswith(".") for part in parts):
        return False
    low = [part.lower() for part in parts]
    return not (_INSTRUCTION_DIRS.intersection(low) or low[-1] in _INSTRUCTION_FILES or low[-1].endswith(".pth"))


def _path_arg_ok(tool_name: str, args: dict) -> bool:
    """Read, attach_file и Grep: путь без учётных данных и без glob. Для Grep путь необязателен."""
    key, required = _PATH_ARG_TOOLS[tool_name]
    if key not in args or (args[key] is None and not required):
        return not required
    return not _credential_path(args[key])


def permission_class(tool_name, args) -> str:
    """'blocked' - только владелец вручную; 'safe' - читающее или ограниченное, годится любое
    подходящее правило (и mac_full_control для mac); 'unknown' - не из белого списка: только
    точное правило с непустым match."""
    if not isinstance(tool_name, str) or not isinstance(args, dict):
        return "blocked"
    label, blocked = _analyze(tool_name, args)
    if blocked or label in HARD_RISKS:
        return "blocked"
    low = tool_name.lower()
    if is_browser_tool(tool_name):
        return "safe" if args.get("action") in _BROWSER_READ_ACTIONS else "unknown"
    mac = _mac_tool(low)
    if mac:
        if low not in (f"mac_{mac}", f"mcp__bothub__mac_{mac}"):
            return "blocked"  # чужой сервер под именем mac_*
        # сюда доходят читающие mac-инструменты и shell, целиком признанный читающим
        # (остальное _analyze уже пометил blocked)
        return "safe"
    if tool_name in READ_ONLY_TOOLS:
        return "safe" if tool_name not in _PATH_ARG_TOOLS or _path_arg_ok(tool_name, args) else "blocked"
    if tool_name in HOME_WRITE_TOOLS:
        return "safe" if _home_path_ok(args) else "blocked"
    return "unknown"


def rule_matches(rule, tool_name: str, args) -> bool:
    """Имя инструмента: fnmatch (как и раньше). Аргументы: точное равенство, без glob."""
    if not isinstance(rule, dict) or not isinstance(args, dict) or not isinstance(tool_name, str):
        return False
    if rule.get("scope") == "browser_origin":
        return _browser_rule_matches(rule, tool_name, args)
    pattern = rule.get("tool")
    if not isinstance(pattern, str) or not pattern or not fnmatchcase(tool_name, pattern):
        return False
    match = rule.get("match")
    if match is None:
        match = {}
    if not isinstance(match, dict):
        return False
    if not all(k in args and _arg_text(args[k]) == _arg_text(v) for k, v in match.items()):
        return False
    pinned = rule.get("op_hash")
    # правило, сохранённое по "запомнить", несёт хэш операции: подмножество ключей match
    # больше не пропускает вызов с дополнительными аргументами. Правило с match без op_hash
    # (старое «запомнить») не действует: его не отличить от подмножества аргументов.
    if match and not pinned:
        return False
    return pinned is None or (pinned == op_hash(tool_name, args))


def _pinned(rule: dict, tool_name: str) -> bool:
    match = rule.get("match")
    return (rule.get("tool") == tool_name and not _GLOB_CHARS.intersection(tool_name)
            and isinstance(match, dict) and bool(match))


def decide(bot: dict, tool_name: str, args) -> tuple[str, bool]:
    risk = classify(tool_name, args)
    kind = permission_class(tool_name, args)
    if risk in HARD_RISKS or kind == "blocked":
        return risk, False
    if kind == "safe" and is_browser_tool(tool_name):
        return risk, True  # snapshot, screenshot: только чтение страницы
    if kind == "safe" and bot.get("mac_full_control") and _mac_tool(tool_name.lower()):
        return risk, True
    rules = bot.get("auto_allow")
    for rule in rules if isinstance(rules, (list, tuple)) else ():
        if rule_matches(rule, tool_name, args) and (kind == "safe" or _pinned(rule, tool_name)):
            return risk, True
    return risk, False


def remember_rule(tool_name, args) -> dict | None:
    """Правило для "запомнить" после одобрения. None, если запоминать нельзя: pay/delete/login,
    установка ПО, отправка наружу из Bash, не читающее на Mac, непонятная команда. Правило
    фиксирует все аргументы точно и несёт хэш операции (без него rule_matches правило не примет)."""
    if not isinstance(tool_name, str) or not tool_name or _GLOB_CHARS.intersection(tool_name):
        return None
    if tool_name == PROCEDURE_TOOL:
        return None  # шаг процедуры спрашивает владельца на каждом запуске (раздел 14), правило ему не нужно
    if not isinstance(args, dict) or permission_class(tool_name, args) == "blocked":
        return None
    if is_browser_tool(tool_name):
        key = browser_rule_key(args)
        return None if key is None else {"tool": tool_name, "scope": "browser_origin",
                                         "match": {"action": key[0], "origin": key[1]}}
    return {"tool": tool_name, "match": {str(k): _arg_text(v) for k, v in args.items()}, "op_hash": op_hash(tool_name, args)}
