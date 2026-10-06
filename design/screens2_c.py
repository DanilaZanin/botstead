"""Экраны этапа 2, часть В: первый вход, экран-хаб настроек, мастер нового бота (шаги 1 и 3; шаг 2 это AvatarPick)."""
# ruff: noqa: F403,F405,E731
from screens2_lib import *  # noqa: F401,F403
from screens2_a import FULL, lab


def nav_row(icon, title, sub, href, tone='neutral', right=''):
    return f'<a href="{href}" style="flex-grow: 1; min-width: 0; min-height: 44px; display: flex; align-items: center; gap: 12px; text-decoration: none; color: var(--fg-default);">{tile(icon, tone)}{two(title, sub)}{right}<span style="color: var(--fg-muted); display: flex;">{ic("chev", 16, 2.2)}</span></a>'


# ---------- Первый вход ----------
def todo(n, st, title, sub, action=''):
    mark = f'<span style="color: var(--success-fg); display: flex;">{ic("check", 16, 2.6)}</span>' if st == 'done' else f'<span style="font: 600 13px/16px {MONO};">{n}</span>'
    return f'<span aria-hidden="true" style="width: 32px; height: 32px; flex-shrink: 0; border-radius: 16px; background: var(--bg-sunken); color: var(--fg-default); display: flex; align-items: center; justify-content: center;">{mark}</span><span style="flex-grow: 1; min-width: 0; display: flex; flex-direction: column; gap: 2px; {"opacity: 0.55;" if st == "locked" else ""}"><span style="{CALLB} color: var(--fg-default);">{title}</span><span style="{FN}">{sub}</span></span>{action}'


def welcome(v):
    head = f'<div style="display: flex; flex-direction: column; gap: 6px; flex-shrink: 0;"><h1 style="margin: 0; {LARGE}">Аккаунт создан</h1><span style="{CALL} color: var(--fg-default);">alice на сервере bots.example.org. До первого бота два шага.</span></div>'
    wrap = lambda inner, b: sbar() + scroll(head + inner, 'padding: 16px 24px; gap: 16px;') + b
    later = btn('ghost', 'Позже', href='Main.dc.html')
    if v == 'load':
        return wrap(loading('Проверяю, какие модели доступны', 3, 64), bottom(later))
    if v == 'err':
        return sbar() + error('Не удалось проверить модели', 'Аккаунт создан, вход выполнен. Список моделей можно открыть позже в настройках.', btn('secondary', 'Повторить', 'refresh', go='none') + btn('ghost', 'К ботам', href='Main.dc.html'))
    if v == 'has':
        lst = rows([todo(1, 'done', 'Модель подключена', 'Claude Code · 3 модели'), todo(2, 'next', 'Создать бота', 'Словами описать, что он должен делать'), todo(3, 'next', 'Добавить на экран «Домой»', 'Так приходят уведомления о решениях')], 12)
        return wrap(lst, bottom(btn('primary', 'Создать бота', 'plus', href='NewBotDescribe.dc.html') + later))
    lst = rows([todo(1, 'next', 'Подключить модель', 'API-ключ, свой endpoint или подписка Claude, ChatGPT, Google'), todo(2, 'locked', 'Создать бота', 'Сначала нужна хотя бы одна модель'), todo(3, 'next', 'Добавить на экран «Домой»', 'Так приходят уведомления о решениях')], 12)
    note = banner('neutral', 'info', 'Моделей пока нет', 'Модели у каждого пользователя свои: чужие ключи и подписки не используются.')
    return wrap(lst + note, bottom(btn('primary', 'Подключить модель', 'plug', href='ProviderAdd.dc.html') + later))


page('Welcome.dc.html', 'Первый вход', [(k, t, welcome(k)) for k, t in [('none', 'пусто: моделей нет'), ('has', 'модель есть'), ('load', 'загрузка'), ('err', 'ошибка')]])


# ---------- Настройки: точки входа на телефоне ----------
def settings(v):
    admin = v != 'member'
    h = hdr_back('Настройки', ('admin · администратор' if admin else 'alice · участник'), href='Main.dc.html')
    if v == 'load':
        return h + scroll(loading('Загружаю настройки', 5, 64))
    if v == 'err':
        return h + error('Настройки не загрузились', 'Сервер не ответил за 10 с. Боты продолжают работать.', btn('secondary', 'Повторить', 'refresh', go='admin'))
    main = [nav_row('plug', 'Провайдеры и модели', '5 подключено · 2 требуют внимания' if admin else '1 подключён', 'Providers.dc.html', 'attention' if admin else 'neutral'), nav_row('brain', 'Память', '42 записи · у каждого бота своя' if admin else '6 записей', 'MemoryEntries.dc.html'),
            nav_row('sliders', 'Разрешения', 'Что боты делают сами', 'Permissions.dc.html'), nav_row('list', 'Активность', 'Что делали боты', 'Activity.dc.html')]
    srv = section('Сервер') + rows([nav_row('user', 'Пользователи', '3 пользователя · 2 инвайта', 'AdminUsers.dc.html')]) if admin else f'<span style="{FN}">Пользователями и инвайтами управляет администратор сервера: Дмитрий.</span>'
    acc = section('Аккаунт') + rows([nav_row('key', 'Сменить пароль', 'Последняя смена: 12 сен', '#'), f'<button type="button" style="flex-grow: 1; min-height: 44px; padding: 0; border: none; background: transparent; text-align: left; font: 500 15px/20px {SANS}; color: var(--danger-fg);">Выйти</button>'])
    return h + scroll(section('Боты') + rows(main) + srv + acc, 'gap: 10px;')


page('Settings.dc.html', 'Настройки', [(k, t, settings(k)) for k, t in [('admin', 'администратор'), ('member', 'участник'), ('load', 'загрузка'), ('err', 'ошибка')]])

# ---------- Мастер нового бота: шаг 1 ----------
TASK = 'Каждое утро искать вакансии SRE с релокацией и показывать пять лучших'


def area(label, value, ph, err='', disabled=False):
    i = uid()
    note = f'<span style="display: flex; align-items: center; gap: 6px; {FN} color: var(--danger-fg);">{ic("risk", 14, 2.4)}{err}</span>' if err else ''
    return f'<div style="display: flex; flex-direction: column; gap: 6px; flex-shrink: 0;"><label for="{i}" style="{FN}">{label}</label><textarea id="{i}" rows="4" placeholder="{ph}"{" disabled" if disabled else ""} style="box-sizing: border-box; padding: 10px 14px; border-radius: 12px; border: 1px solid var(--border-control); background: var(--bg-surface); color: var(--fg-default); font: 400 15px/20px {SANS}; resize: none; {"opacity: 0.6;" if disabled else ""}">{value}</textarea>{note}</div>'


def nb1(v):
    h = hdr_back('Новый бот', 'Шаг 1 из 3: задача', href='Main.dc.html', back_icon='x', back_label='Закрыть мастер')
    busy = v == 'load'
    ex = ''.join(f'<button type="button" style="min-height: 44px; padding: 0 14px; border-radius: 22px; border: 1px solid var(--border-control); background: var(--bg-surface); color: var(--fg-default); font: 500 14px/18px {SANS};">{t}</button>' for t in ['Следить за серверами', 'Разбирать почту', 'Искать файлы на Mac'])
    top = banner('danger', 'risk', 'Описание не разобрано', 'Модель не ответила за 30 с. Текст сохранён, можно повторить.') if v == 'err' else ''
    body = (top + area('Что должен делать бот', '' if v == 'empty' else TASK, 'Например: каждое утро проверять серверы и писать, что сломалось', disabled=busy)
            + lab('Примеры', f'<div style="display: flex; flex-wrap: wrap; gap: 8px;">{ex}</div>') + lab('Где работает', seg(['Сервер', 'Mac'], 0, 'Где работает'))
            + f'<span style="{FN}">Бот получит свой компьютер: отдельный браузер, файлы и память. Имя, модель и разрешения на шаге 3.</span>')
    if busy:
        b = btn('primary', 'Собираю бота из описания', busy=True, disabled=True)
    elif v == 'empty':
        b = btn('primary', 'Дальше', disabled=True)
    else:
        b = btn('primary', 'Повторить' if v == 'err' else 'Дальше', href='AvatarPick.dc.html')
    return h + scroll(body) + bottom(b)


page('NewBotDescribe.dc.html', 'Новый бот, шаг 1: задача', [(k, t, nb1(k)) for k, t in [('form', 'форма'), ('empty', 'пусто'), ('load', 'загрузка'), ('err', 'ошибка')]])


# ---------- Мастер нового бота: шаг 3 ----------
def pick_row(label, value, href, extra=''):
    return lab(label, f'<a href="{href}" style="min-height: 44px; display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 0 14px; border-radius: 12px; border: 1px solid var(--border-control); background: var(--bg-surface); color: var(--fg-default); text-decoration: none; font: 500 15px/20px {SANS};"><span style="display: flex; align-items: center; gap: 8px; min-width: 0;">{value}{extra}</span>{ic("chev", 16, 2.4)}</a>')


def nb3(v):
    h = hdr_back('Новый бот', 'Шаг 3 из 3: проверка', href='AvatarPick.dc.html')
    busy, none, e = v == 'load', v == 'none', v == 'err'
    top = banner('danger', 'risk', 'Бот не создан: компьютер не запустился', 'Описание, персонаж и имя сохранены. Можно повторить.') if e else ''
    who = f'<div style="display: flex; align-items: center; gap: 14px; flex-shrink: 0;">{avatars.avatar_html("owl", None if none else "claude", 64)}<div style="flex-grow: 1; min-width: 0;">{field("Имя", "Сова", disabled=busy)}</div></div>'
    role = card(f'<span style="{FN}">Задача</span><span style="{CALL} color: var(--fg-default);">{TASK}</span><span style="{FN}">Работает на сервере · расписание: каждый день 08:00</span>', gap=4)
    if none:
        model = banner('attention', 'plug', 'Нет подключённых моделей', 'Без модели бот не сможет отвечать. Черновик бота сохранится.') + btn('secondary', 'Подключить модель', 'plug', href='ProviderAdd.dc.html')
    else:
        model = pick_row('Модель', 'Sonnet 5', 'BotModel.dc.html', badge('claude', 'Claude Code'))
    perms = pick_row('Разрешения', 'Браузер и терминал: спросить', 'Permissions.dc.html')
    note = f'<span style="{FN}">Пароли, оплата, удаление и установка программ всегда с подтверждением.</span>'
    if busy:
        b = btn('primary', 'Создаю компьютер бота', busy=True, disabled=True)
    elif none:
        b = btn('primary', 'Создать бота', disabled=True)
    else:
        b = btn('primary', 'Повторить' if e else 'Создать бота', href='Thread.dc.html')
    return h + scroll(top + who + role + model + perms + note) + bottom(b)


page('NewBotConfirm.dc.html', 'Новый бот, шаг 3: проверка', [(k, t, nb3(k)) for k, t in [('data', 'данные'), ('none', 'пусто: моделей нет'), ('load', 'загрузка'), ('err', 'ошибка')]])
