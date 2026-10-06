"""Конструктор ботов. Контракт: docs/contracts.md, раздел 9.

`launcher_drafter` запускает Claude через лаунчер в контейнере владельца.
`validate_draft` — нормализация сырого BotDraft; никогда не бросает исключение,
невалидные поля заменяются безопасными значениями, исправления идут в rationale.
502 builder_failed (см. bothub/main.py:/api/bots/draft) — только когда drafter()
сам не смог вернуть dict (не JSON, ошибка CLI, таймаут).
"""
from __future__ import annotations

import json
import re
from bothub.launcher_client import run_exec

from croniter import croniter

from bothub.risk import MAC_RISKY_TOOLS, classify

VALID_AVATARS = ('scout', 'mac', 'sre', 'coder', 'archive', 'owl', 'spark', 'robot', 'fox', 'cat')
VALID_MODELS = ('claude-haiku-4-5-20251001', 'claude-sonnet-5', 'claude-opus-5-5')
DEFAULT_MODEL = 'claude-sonnet-5'
DEFAULT_TIMEZONE = 'Europe/Moscow'
DRAFT_TIMEOUT_SECONDS = 120

# Раздел 9: инструкции черновика всегда заканчиваются этим блоком (текст с прода).
COMMON_INSTRUCTIONS = (
    "Мозг у тебя Claude. Если задача требует большого ресёрча, длинного чтения или кода "
    "и Mac владельца в сети, отдай подзадачу через инструмент mac_delegate (engine=gemini "
    "для ресёрча и чтения, codex для кода, claude для второго мнения), затем проверь и "
    "сведи результат. Если mac_delegate вернул, что Mac не в сети, делай задачу сам. "
    "Необратимые действия (отправка, оплата, удаление, вход) спрашивают владельца через "
    "approve. Результат подтверждай фактом: путь к файлу, вывод команды, скриншот. Пиши "
    "по-русски, коротко."
)

# Инструменты, которые конструктору разрешено класть в auto_allow (раздел 4: только
# чтение может авто-разрешаться заранее без владельца в комнате).
_ALLOWED_AUTO_ALLOW_TOOLS = (
    'mcp__bothub__mac_find_files', 'mcp__bothub__mac_read_file', 'mcp__bothub__mac_preview',
    'mcp__bothub__mac_screenshot', 'mcp__bothub__mac_upload_file', 'mcp__bothub__attach_file',
    'mcp__bothub__remember',
)

# Базовые правила для любого нового бота: чтение, веб-поиск и запись
# файлов. Bash и WebFetch сюда не входят: они всегда через подтверждение. Write/Edit
# авто-разрешаются только внутри /home/bot без скрытых каталогов (bothub.risk._home_path_ok),
# само правило путь не ограничивает.
BASE_AUTO_ALLOW = (
    'Read', 'Glob', 'Grep', 'TodoWrite', 'WebSearch', 'Write', 'Edit',
    'mcp__bothub__remember', 'mcp__bothub__attach_file',
)

BUILDER_SYSTEM_PROMPT = f"""Ты конструктор ботов Bot Hub — личного self-hosted аналога Grok Bot.
Владелец описывает одного нового бота своими словами, ты переводишь описание в готовую
конфигурацию. Существующие боты не меняются, речь только о новом.

Боты Bot Hub — персонажи с ролью, инструкциями и своим "рабочим местом" (контейнер на сервере
или Mac владельца через Mac-агента). Десять аватаров-персонажей, выбирай по смыслу задачи:
- scout — разведка и ресёрч: сбор информации из открытых источников, мониторинг новостей/цен.
- mac — руки владельца: файлы, приложения, автоматизация на его Mac.
- sre — инфраструктура: серверы, мониторинг, деплой, инциденты.
- coder — код: пишет, чинит, ревьюит, гоняет тесты.
- archive — архив: сортирует и находит документы, ведёт заметки и историю.
- owl — аналитика: сводки, отчёты, наблюдение за метриками.
- spark — рутина: быстрые механические задачи, короткие ответы, черновики текста.
- robot — универсальный помощник, когда ни один персонаж не подходит точно.
- fox — находчивый помощник: нестандартные задачи, поиск обходных путей, быстрые решения.
- cat — спокойный компаньон: неспешные ежедневные дела, напоминания, личные заметки и разговор.

Модель (`model`) выбирай по сложности задач бота:
- claude-haiku-4-5-20251001 — простая рутина, сортировка, короткие механические ответы.
- claude-sonnet-5 — по умолчанию для большинства ботов.
- claude-opus-5-5 — сложный анализ, разбор инцидентов, задачи с высокой ценой ошибки.

`executor` — "container" по умолчанию. Ставь `executor: "mac"` или хотя бы
`mac_full_control: true` (при `executor: "container"`), только если из описания явно
следует, что боту нужно работать с файлами или приложениями на Mac владельца.

`auto_allow` — правила `{{"tool": "..."}}`, которые разрешают действие без спроса у
владельца. Разрешены только инструменты чтения из этого списка (не выдумывай другие,
никогда не добавляй shell/applescript/open/click/type_text/move_to_trash — необратимые
и опасные действия всегда спрашивают владельца через approve):
{', '.join(_ALLOWED_AUTO_ALLOW_TOOLS)}.
Добавляй только то, что реально нужно по описанию, не все сразу.

`schedule` — только если в описании явно есть регулярность (каждый день, по утрам, раз в
неделю и т.п.). Иначе `schedule: null`. `cron` в форме "mm hh dd MM DOW",
`timezone` по умолчанию "Europe/Moscow".

Ответь строго одним JSON-объектом без пояснений и без markdown-обёртки, по схеме:
{{"id": "slug-a-z0-9-", "name": "короткое имя бота по-русски (1-2 слова, например Вакансии или Налоговик)", "role": "одна фраза", "avatar": "один из десяти",
 "provider": "claude", "model": "...", "executor": "container|mac", "mac_full_control": bool,
 "auto_allow": [{{"tool": "..."}}], "instructions": "инструкции боту",
 "schedule": {{"name": "...", "cron": "...", "timezone": "...", "prompt": "..."}} | null,
 "rationale": "2-3 предложения, почему такие настройки"}}
"""


def extract_json(text: str) -> dict:
    """Первый JSON-объект в тексте (модель иногда оборачивает ответ в markdown/пояснения)."""
    decoder = json.JSONDecoder()
    start = text.find('{')
    while start != -1:
        try:
            obj, _ = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            start = text.find('{', start + 1)
            continue
        if isinstance(obj, dict):
            return obj
        start = text.find('{', start + 1)
    raise ValueError('no JSON object found in drafter output')


async def launcher_drafter(description: str, launcher, owner_id: str) -> dict:
    """Раннер по умолчанию для POST /api/bots/draft (раздел 9): claude -p внутри
    уже запущенного контейнера бота, системная инструкция конструктора флагом,
    описание владельца через stdin, ответ парсится из --output-format json."""
    bots = await launcher.list_bots()
    candidate = next((b for b in bots if b.owner_id == owner_id and b.running), None)
    temporary = False
    if candidate is None:
        import secrets as std_secrets
        bot_id = 'draft-' + std_secrets.token_hex(6)
        candidate = await launcher.create_bot(bot_id, owner_id)
        temporary = True
    try:
        result = await run_exec(launcher, candidate.bot_id,
            ['claude', '-p', '--model', DEFAULT_MODEL, '--output-format', 'json',
             '--tools', '', '--system-prompt', BUILDER_SYSTEM_PROMPT],
            stdin=description, timeout=DRAFT_TIMEOUT_SECONDS)
        if result.code != 0:
            raise RuntimeError('builder CLI exited unsuccessfully')
        reply = json.loads(result.stdout)
    finally:
        if temporary:
            await launcher.remove_bot(candidate.bot_id, purge=True)
    result = reply.get('result', '') if isinstance(reply, dict) else str(reply)
    return extract_json(result)


def _slugify(text: str) -> str:
    slug = re.sub(r'[^a-z0-9-]+', '-', (text or '').strip().lower()).strip('-')
    return (slug or 'bot')[:32]


def _unique_id(base: str, existing: set[str]) -> str:
    if base not in existing:
        return base
    n = 2
    while True:
        suffix = f'-{n}'
        candidate = base[:32 - len(suffix)] + suffix
        if candidate not in existing:
            return candidate
        n += 1


def _valid_cron(cron, timezone) -> bool:
    if not isinstance(cron, str) or not cron.strip():
        return False
    try:
        croniter(cron)
    except Exception:
        return False
    return isinstance(timezone, str) and bool(timezone)


def _clean_auto_allow(rules, notes: list[str]) -> list[dict]:
    if not isinstance(rules, list):
        if rules:
            notes.append('auto_allow не был списком, очищен')
        return []
    cleaned = []
    dropped = 0
    for rule in rules:
        tool = rule.get('tool') if isinstance(rule, dict) else None
        # Раздел 4/9: только читающие mcp__bothub__* инструменты из белого списка
        # конструктора, риск other (classify), никогда mac_shell/applescript/... -
        # даже без args, эти инструменты слишком опасны для авто-разрешения заранее.
        if (isinstance(tool, str) and tool in _ALLOWED_AUTO_ALLOW_TOOLS
                and not any(tool == f'mcp__bothub__mac_{risky}' for risky in MAC_RISKY_TOOLS)
                and classify(tool, {}) == 'other'):
            clean = {'tool': tool}
            match = rule.get('match')
            if isinstance(match, dict) and match:
                clean['match'] = {str(k): str(v) for k, v in match.items()}
            cleaned.append(clean)
        else:
            dropped += 1
    if dropped:
        notes.append(f'{dropped} правил auto_allow отклонены: разрешены только читающие mcp__bothub__* инструменты')
    return cleaned


def validate_draft(raw: dict, existing_ids: set[str]) -> dict:
    """Нормализует сырой BotDraft по разделу 9. Не бросает исключений: любое
    невалидное поле заменяется безопасным значением, исправления идут в rationale."""
    raw = raw if isinstance(raw, dict) else {}
    notes: list[str] = []

    raw_id = raw.get('id')
    if isinstance(raw_id, str) and re.fullmatch(r'[a-z0-9-]{1,32}', raw_id):
        base_id = raw_id
    else:
        base_id = _slugify(raw_id if isinstance(raw_id, str) else raw.get('name', ''))
        notes.append(f'id "{raw_id}" не проходил формат slug, заменён на "{base_id}"')
    bot_id = _unique_id(base_id, existing_ids or set())
    if bot_id != base_id:
        notes.append(f'id "{base_id}" уже занят, использован "{bot_id}"')

    name = raw.get('name')
    name = name.strip() if isinstance(name, str) and name.strip() else bot_id
    role = raw.get('role') if isinstance(raw.get('role'), str) else ''
    if len(role) > 512:  # предел BotIn.role: черновик не должен отклоняться при создании бота
        role = role[:512].rstrip(); notes.append('роль сокращена до 512 символов')

    avatar = raw.get('avatar')
    if avatar not in VALID_AVATARS:
        notes.append(f'avatar "{avatar}" неизвестен, поставлен "robot"')
        avatar = 'robot'

    if raw.get('provider') != 'claude':
        notes.append(f'provider "{raw.get("provider")}" заменён на claude: мозг ботов только claude')
    provider = 'claude'

    model = raw.get('model')
    if model not in VALID_MODELS:
        notes.append(f'model "{model}" неизвестна, поставлена "{DEFAULT_MODEL}"')
        model = DEFAULT_MODEL

    executor = raw.get('executor')
    if executor not in ('container', 'mac'):
        notes.append(f'executor "{executor}" неизвестен, поставлен "container"')
        executor = 'container'

    mac_full_control = raw.get('mac_full_control') is True

    auto_allow = _clean_auto_allow(raw.get('auto_allow'), notes)
    present = {r['tool'] for r in auto_allow}
    auto_allow = [{'tool': t} for t in BASE_AUTO_ALLOW if t not in present] + auto_allow

    instructions = raw.get('instructions') if isinstance(raw.get('instructions'), str) else ''
    if COMMON_INSTRUCTIONS not in instructions:
        instructions = (instructions.rstrip() + '\n\n' + COMMON_INSTRUCTIONS).strip()
        notes.append('добавлен общий блок инструкций')

    schedule = raw.get('schedule')
    if isinstance(schedule, dict):
        cron = schedule.get('cron')
        tz = schedule.get('timezone') if isinstance(schedule.get('timezone'), str) and schedule.get('timezone') else DEFAULT_TIMEZONE
        prompt = schedule.get('prompt')
        if _valid_cron(cron, tz) and isinstance(prompt, str) and prompt.strip():
            sched_name = schedule.get('name')
            schedule = {
                # пределы как у создания бота: имя до 512 символов, prompt до 16 КиБ
                'name': (sched_name.strip() if isinstance(sched_name, str) and sched_name.strip() else name)[:512],
                'cron': cron, 'timezone': tz, 'prompt': prompt[:16*1024],
            }
        else:
            notes.append('schedule отклонён: невалидный cron или пустой prompt')
            schedule = None
    else:
        if schedule is not None:
            notes.append('schedule не был объектом, заменён на null')
        schedule = None

    rationale = raw.get('rationale')
    rationale = rationale.strip() if isinstance(rationale, str) and rationale.strip() else ''
    if notes:
        rationale = (rationale + (' ' if rationale else '') + 'Исправления: ' + '; '.join(notes)).strip()
    if not rationale:
        rationale = 'Настройки по умолчанию.'

    return {
        'id': bot_id, 'name': name, 'role': role, 'avatar': avatar,
        'provider': provider, 'model': model, 'executor': executor,
        'mac_full_control': mac_full_control, 'auto_allow': auto_allow,
        'instructions': instructions, 'schedule': schedule, 'rationale': rationale,
    }
