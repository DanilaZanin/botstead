"""Экраны этапа 2, часть А: вход и инвайты, пользователи, провайдеры и модели, терминал входа по подписке."""
# ruff: noqa: F403,F405,E731
from screens2_lib import *  # noqa: F401,F403


def lab(label, inner):
    return f'<div style="display: flex; flex-direction: column; gap: 6px; flex-shrink: 0;"><span style="{FN}">{label}</span>{inner}</div>'


def grid(inner, cols='repeat(2, minmax(0, 1fr))'):
    return f'<div style="display: grid; grid-template-columns: {cols}; gap: 8px; flex-shrink: 0;">{inner}</div>'


FULL = 'width: 100%;'

# ---------- 1. Вход ----------
def login(v):
    top = banner('danger', 'cloudoff', 'Сервер не отвечает', 'Данные не отправлены. Проверьте сеть или VPN.') if v == 'down' else ''
    err = 'Логин или пароль не подходят' if v == 'err' else ''
    busy = v == 'busy'
    b = btn('primary', 'Вхожу', busy=True, disabled=True, extra=FULL) if busy else (btn('primary', 'Повторить', 'refresh', go='form', extra=FULL) if v == 'down' else btn('primary', 'Войти', go='busy', extra=FULL))
    return (sbar() + f'<main style="flex-grow: 1; display: flex; flex-direction: column; justify-content: center; gap: 16px; padding: 0 24px 80px;">'
            f'<div style="display: flex; flex-direction: column; gap: 4px;"><h1 style="margin: 0; {LARGE}">{BRAND}</h1><span style="{FN}">Вход на сервер bots.example.org</span></div>{top}'
            + field('Логин', 'admin', disabled=busy) + field('Пароль', '000000000000', typ='password', err=err, disabled=busy, right=iconbtn('eye', 'Показать пароль', 'ghost'))
            + b + f'<span style="{FN} text-align: center;">Регистрации нет. Доступ выдаёт администратор по инвайту.</span></main>')


page('Login.dc.html', 'Вход', [('form', 'форма', login('form')), ('busy', 'загрузка', login('busy')), ('err', 'ошибка: пароль', login('err')), ('down', 'ошибка: сервер', login('down'))])


def invite(v):
    if v == 'load':
        return sbar() + scroll(f'<h1 style="margin: 0; {LARGE}">Приглашение</h1>' + loading('Проверяю приглашение', 3, 60), 'padding: 24px;')
    if v == 'expired':
        return sbar() + empty('clock', 'Инвайт истёк', 'Ссылка действовала до 3 окт, 14:00 и больше не работает. Новую выдаёт администратор сервера.', btn('secondary', 'Уже есть аккаунт: войти', href='Login.dc.html'))
    if v == 'used':
        return sbar() + empty('link', 'Инвайт уже использован', 'По этой ссылке аккаунт создан 2 окт. Ссылка одноразовая.', btn('primary', 'Войти', href='Login.dc.html'))
    e = v == 'err'
    return (sbar() + f'<main style="flex-grow: 1; display: flex; flex-direction: column; gap: 14px; padding: 16px 24px;">'
            f'<h1 style="margin: 0; {LARGE}">Приглашение</h1><span style="{CALL} color: var(--fg-default);">Дмитрий приглашает на сервер bots.example.org. Роль: участник.</span><div>{tag("Действует до 6 окт, 14:00", "neutral", "clock")}</div>'
            + field('Логин', 'alice', hint='' if e else 'Латиница и цифры, от 3 символов', err='Логин alice уже занят' if e else '')
            + field('Пароль', '000000000000', typ='password', hint='От 12 символов', right=iconbtn('eye', 'Показать пароль', 'ghost'))
            + field('Пароль ещё раз', '' if not e else '0000000000', typ='password', err='Пароли не совпадают' if e else '')
            + btn('primary', 'Создать аккаунт', href='Welcome.dc.html', extra=FULL) + f'<span style="{FN} text-align: center;">Боты, модели и память у каждого пользователя свои.</span></main>')


page('Invite.dc.html', 'Принятие инвайта', [('form', 'форма', invite('form')), ('load', 'загрузка', invite('load')), ('expired', 'инвайт истёк', invite('expired')), ('used', 'инвайт использован', invite('used')), ('err', 'ошибка ввода', invite('err'))])

# ---------- 1б. Админ: пользователи и инвайты ----------
def person(letter):
    return f'<span aria-hidden="true" style="width: 40px; height: 40px; flex-shrink: 0; border-radius: 20px; background: var(--bg-sunken); color: var(--fg-default); display: flex; align-items: center; justify-content: center; {CALLB}">{letter}</span>'


USERS = [('Д', 'Дмитрий', 'админ', '5 ботов', 'сегодня 09:41', status('success-fg', 'В сети')), ('А', 'Алиса', 'участник', '2 бота', 'вчера 22:10', status('neutral-fg', 'Не в сети')), ('О', 'Борис', 'участник', '1 бот', '12 сен', status('danger-fg', 'Отключён', 'ban'))]
INV = [('link', 'Для Миши', 'участник · истекает через 46 ч', 'Отозвать'), ('clock', 'Для Кати', 'участник · истёк 3 окт', 'Заново')]


def users(v):
    h = hdr_back('Пользователи', 'Сервер bots.example.org', href='Settings.dc.html')
    add = bottom(btn('primary', 'Создать инвайт', 'plus', go='new'))
    urows = [person(l) + two(n, f'{r} · {b}', st) + iconbtn('more', f'Действия: {n}', 'muted') for l, n, r, b, _, st in USERS]
    irows = [tile(i) + two(n, s) + btn('ghost', a) for i, n, s, a in INV]
    data = h + scroll(section('Пользователи · 3') + rows(urows) + section('Инвайты · 2') + rows(irows) + f'<span style="{FN}">Инвайт одноразовый. Приглашённый видит только своих ботов и свои модели.</span>') + add
    if v == 'empty':
        return h + scroll(section('Пользователи · 1') + rows(urows[:1]) + empty('link', 'Пока здесь только администратор', 'Инвайт даёт одноразовую ссылку со сроком. Регистрации без ссылки нет.')) + add
    if v == 'load':
        return h + scroll(loading('Загружаю пользователей')) + add
    if v == 'err':
        return h + error('Список не загрузился', 'Сервер ответил 500. Пользователи и инвайты не изменены.', btn('secondary', 'Повторить', 'refresh', go='data'))
    if v == 'new':
        return data + sheet('Новый инвайт', field('Для кого', 'Миша', hint='Заметка для себя, приглашённый её не увидит') + lab('Роль', seg(['Участник', 'Админ'], 0, 'Роль')) + lab('Срок ссылки', seg(['1 день', '3 дня', '7 дней'], 1, 'Срок ссылки'))
                            + grid(btn('secondary', 'Отмена', go='data') + btn('primary', 'Создать ссылку', go='made')), close='data')
    if v == 'made':
        box = f'<div style="padding: 12px 14px; border-radius: 12px; background: var(--bg-sunken); {LOG} color: var(--fg-default); overflow-wrap: anywhere;">https://bots.example.org/i/7Fq2-Lm8x-Rt41</div>'
        return data + sheet('Ссылка готова', box + banner('attention', 'risk', 'Ссылка показывается один раз', 'Одноразовая, действует до 7 окт, 14:00.') + grid(btn('secondary', 'Поделиться', 'ext') + btn('primary', 'Скопировать', 'copy', go='data')), sub='Инвайт для Миши · участник', close='data')
    return data


page('AdminUsers.dc.html', 'Пользователи и инвайты', [(k, t, users(k)) for k, t in [('data', 'данные'), ('new', 'новый инвайт'), ('made', 'ссылка готова'), ('empty', 'пусто'), ('load', 'загрузка'), ('err', 'ошибка')]])


def users_d(v):
    cols = '2fr 1fr 0.8fr 1.2fr 1.3fr 44px'
    head = ['Пользователь', 'Роль', 'Боты', 'Последний вход', 'Состояние', '']
    trows = [[person(l) + f'<span style="{CALLB}">{n}</span>', r, b, t, st, iconbtn('more', f'Действия: {n}', 'muted')] for l, n, r, b, t, st in USERS]
    inv = ''.join(card(f'<div style="display: flex; align-items: center; gap: 10px;">{tile(i)}{two(n, s)}{btn("ghost", a)}</div>', '10px 12px') for i, n, s, a in INV)
    form = card(f'<span style="{CALLB}">Новый инвайт</span>' + field('Для кого', '', 'Имя или заметка') + lab('Роль', seg(['Участник', 'Админ'], 0, 'Роль')) + lab('Срок ссылки', seg(['1 день', '3 дня', '7 дней'], 1, 'Срок ссылки')) + btn('primary', 'Создать ссылку', 'link'))
    aside = f'<h2 style="margin: 0; {HEAD}">Инвайты</h2>' + (inv if v == 'data' else f'<span style="{FN}">Активных инвайтов нет.</span>') + form
    if v == 'load':
        body, aside = loading('Загружаю пользователей', 4, 56), loading('Загружаю инвайты', 2, 56)
    elif v == 'err':
        body = error('Список не загрузился', 'Сервер ответил 500. Пользователи и инвайты не изменены.', btn('secondary', 'Повторить', 'refresh', go='data'))
    elif v == 'empty':
        body = table(head, trows[:1], cols) + empty('link', 'Пока здесь только администратор', 'Инвайт даёт одноразовую ссылку со сроком. Форма справа.')
    else:
        body = table(head, trows, cols) + f'<span style="{FN}">Отключённый пользователь не может войти, его боты остановлены, данные сохранены.</span>'
    return desk('users', 'Пользователи', 'Сервер bots.example.org · регистрация только по инвайту', '', body, aside)


page('AdminUsersDesktop.dc.html', 'Пользователи на Mac', [(k, t, users_d(k)) for k, t in [('data', 'данные'), ('empty', 'пусто'), ('load', 'загрузка'), ('err', 'ошибка')]], DW, DH)

# ---------- 2. Провайдеры и модели ----------
PROV = [('claude', 'term', 'Claude Code', 'Подписка · 3 модели', status('success-fg', 'Вход выполнен'), 'ProviderModels.dc.html'),
        ('claude', 'key', 'Anthropic API', 'Ключ sk-ant-…4f2a · 2 модели', status('success-fg', 'Работает · проверен 5 мин назад'), 'ProviderModels.dc.html'),
        ('codex', 'key', 'OpenAI API', 'Ключ sk-…91bc · модели выключены', status('danger-fg', 'Ключ отклонён: 401', 'x'), 'ProviderModels.dc.html'),
        ('neutral', 'globe', 'Ollama дома', '192.168.1.20:11434/v1 · 4 модели', status('attention-fg', 'Не отвечает 3 ч'), 'ProviderModels.dc.html'),
        ('codex', 'term', 'Codex CLI', 'Подписка · моделей нет', status('attention-fg', 'Нужен вход'), 'CliLogin.dc.html')]


def prov_card(tone, icon, name, kind, st, href, sel=False):
    return f'<a href="{href}" style="display: flex; gap: 12px; align-items: center; padding: 12px 14px; {CARD} {"border-color: var(--fg-default);" if sel else ""} text-decoration: none; color: var(--fg-default); flex-shrink: 0;">{tile(icon, tone)}{two(name, kind, st)}<span style="color: var(--fg-muted); display: flex;">{ic("chev", 16, 2.2)}</span></a>'


def providers(v):
    h = hdr_back('Провайдеры', 'Модели для ботов', href='Settings.dc.html')
    add = bottom(btn('primary', 'Добавить провайдера', 'plus', href='ProviderAdd.dc.html'))
    if v == 'empty':
        return h + empty('plug', 'Провайдеров нет', 'Боты не смогут отвечать, пока не подключена хотя бы одна модель: по API-ключу, через свой endpoint или по подписке.') + add
    if v == 'load':
        return h + scroll(loading('Проверяю провайдеров', 5, 76)) + add
    if v == 'err':
        return h + error('Провайдеры не загрузились', 'Сервер не ответил за 10 с. Боты продолжают работать на прежних моделях.', btn('secondary', 'Повторить', 'refresh', go='data'))
    return h + scroll(banner('attention', 'risk', '2 бота без модели', 'Кодер и Архив ждут: их провайдеры недоступны.') + ''.join(prov_card(*p) for p in PROV), 'gap: 10px;') + add


page('Providers.dc.html', 'Провайдеры', [(k, t, providers(k)) for k, t in [('data', 'данные'), ('empty', 'пусто'), ('load', 'загрузка'), ('err', 'ошибка')]])


def fail_block(title, text, tech=''):
    t = f'<details><summary style="min-height: 44px; display: flex; align-items: center; {FN} color: var(--fg-default); cursor: pointer;">Подробнее</summary><span style="{LOG} color: var(--fg-default);">{tech}</span></details>' if tech else ''
    return f'<div role="alert" style="padding: 14px 14px {"4px" if tech else "14px"}; border-radius: 16px; background: var(--danger-bg); display: flex; flex-direction: column; gap: 6px; flex-shrink: 0;"><div style="display: flex; align-items: center; gap: 10px; color: var(--danger-fg); {CALLB}">{ic("x", 20, 2.4)}{title}</div><span style="font: 400 13px/18px {SANS}; color: var(--fg-default);">{text}</span>{t}</div>'


def probe(v):
    if v == 'probing':
        return card(f'<div style="display: flex; align-items: center; gap: 10px;">{spin(18)}{two("Пробный запрос", "Отправил короткий запрос модели, жду ответ")}</div>')
    if v == 'ok':
        return card(f'<div style="display: flex; align-items: center; gap: 10px;"><span style="color: var(--success-fg); display: flex;">{ic("check", 20, 2.4)}</span>{two("Провайдер отвечает", "Найдено моделей: 6")}</div><span style="{LOG} color: var(--fg-muted);">claude-sonnet-5 · 0,8 с · 12 токенов</span>')
    if v == 'fail':
        return fail_block('Ключ отклонён', 'Провайдер не сохранён. Новый ключ создаётся на console.anthropic.com в разделе API Keys.', '401 invalid x-api-key')
    if v == 'ep_down':
        return fail_block('Адрес не отвечает', 'Провайдер не сохранён. Сервер моделей должен быть запущен и доступен с сервера botstead, а не только с этого устройства.', 'connect timeout 10 s · 192.168.1.20:11434')
    if v == 'ep_bad':
        return fail_block('Это не OpenAI-совместимый API', 'Провайдер не сохранён. Адрес ответил, но списка моделей не отдал. Обычно адрес заканчивается на /v1.', 'GET /models: 404 Not Found')
    return ''


def padd(v):
    kind = 'cli' if v == 'cli' else ('endpoint' if v in ('endpoint', 'endpoint_admin', 'ep_down', 'ep_bad') else 'api')
    h = hdr_back('Новый провайдер', 'Подключение и пробный запрос', href='Providers.dc.html')
    sg = seg(['API-ключ', 'Endpoint', 'Подписка'], ['api', 'endpoint', 'cli'].index(kind), 'Вид провайдера', gos=['api', 'endpoint', 'cli'])
    check = btn('primary', 'Проверить и сохранить', go='probing', extra=FULL)
    again = btn('primary', 'Проверить ещё раз', 'refresh', go='probing', extra=FULL)
    if kind == 'api':
        filled = v != 'api'
        body = (lab('Вендор', seg(['Anthropic', 'OpenAI', 'Google'], 0, 'Вендор'))
                + field('API-ключ', '000000000000000000' if filled else '', 'sk-ant-…', typ='password', mono=True, err='Ключ не принят вендором' if v == 'fail' else '', hint='Хранится на сервере зашифрованным. К боту не попадает.', right=iconbtn('paste', 'Вставить из буфера'))
                + field('Название', 'Anthropic API') + probe(v))
        b = {'api': check, 'probing': btn('primary', 'Проверяю', busy=True, disabled=True, extra=FULL), 'ok': btn('primary', 'Выбрать модели', href='ProviderModels.dc.html', extra=FULL), 'fail': again}[v]
    elif kind == 'endpoint':
        url_err = {'ep_down': 'Адрес не ответил за 10 с', 'ep_bad': 'По этому адресу нет списка моделей'}.get(v, '')
        body = field('Название', 'Ollama дома') + field('Base URL', 'http://192.168.1.20:11434/v1', mono=True, err=url_err, hint='Адрес OpenAI-совместимого API. Обычно заканчивается на /v1.') + field('API-ключ, если нужен', '', 'Можно оставить пустым', typ='password', mono=True)
        if v == 'endpoint':
            body += banner('attention', 'lock', 'Адрес из локальной сети закрыт', 'Открыть его может администратор сервера (Дмитрий) в настройках этого провайдера. До этого проверка не пройдёт.')
            b = btn('primary', 'Сохранить и ждать администратора', extra=FULL)
        elif v == 'endpoint_admin':
            body += banner('attention', 'risk', 'Адрес из локальной сети', 'По умолчанию такие адреса закрыты, чтобы бот не добрался до домашних устройств.') + card(switch(True, 'Разрешить этот адрес', 'Видно только администратору. Действует для этого провайдера.'), '4px 14px')
            b = check
        else:
            body += probe(v)
            b = again
    else:
        body = (card(radio(True, 'Claude Code', 'claude login · подписка Claude', badge('claude', 'Claude')) + radio(False, 'Codex CLI', 'codex login · подписка ChatGPT', badge('codex', 'Codex')) + radio(False, 'Antigravity', 'agy · аккаунт Google', badge('gemini', 'Gemini')), gap=4)
                + banner('neutral', 'info', 'Вход идёт в терминале на сервере', 'Данные входа хранятся отдельно для каждого пользователя и доступны только его ботам.'))
        b = btn('primary', 'Открыть терминал входа', 'term', href='CliLogin.dc.html', extra=FULL)
    return h + scroll(sg + body) + bottom(b)


page('ProviderAdd.dc.html', 'Новый провайдер', [(k, t, padd(k)) for k, t in [('api', 'API-ключ'), ('endpoint', 'endpoint: участник'), ('endpoint_admin', 'endpoint: администратор'), ('cli', 'подписка CLI'), ('probing', 'проба: загрузка'), ('ok', 'проба: успех'), ('fail', 'ошибка: ключ отклонён'), ('ep_down', 'ошибка: адрес не отвечает'), ('ep_bad', 'ошибка: не тот API')]])

MODELS = [(True, 'Opus 5.5', 'claude-opus-5-5 · контекст 200k'), (True, 'Sonnet 5', 'claude-sonnet-5 · контекст 200k'), (False, 'Haiku 5', 'claude-haiku-5 · контекст 200k'), (False, 'Opus 5', 'claude-opus-5 · контекст 200k')]


def pmodels(v, desktop=False):
    stat = card(f'<div style="display: flex; align-items: center; gap: 12px;">{two("Пробный запрос", "5 мин назад · 0,8 с", status("success-fg", "Работает"))}{btn("secondary", "Проверить", "refresh")}</div>')
    if v == 'load':
        inner = loading('Запрашиваю список моделей', 4, 60)
    elif v == 'empty':
        inner = stat + empty('list', 'Провайдер не вернул модели', 'Адрес отвечает, но список пуст. Модель можно добавить по имени.') + field('Имя модели', '', 'например llama3.3', mono=True, right=btn('primary', 'Добавить'))
    elif v == 'err':
        inner = (banner('danger', 'x', 'Ключ отклонён: 401', 'Модели выключены. Без модели остались 2 бота: Кодер, Архив.') + btn('secondary', 'Заменить ключ', 'key') + section('Модели · выключены')
                 + rows([switch(False, n, s, disabled=True) for _, n, s in MODELS[:3]]))
    else:
        inner = (stat + section('Модели · включено 2 из 4') + rows([switch(on, n, s) for on, n, s in MODELS])
                 + f'<span style="{FN}">Включённые модели видны в настройках ботов. Выключение не прерывает идущие задачи.</span>' + btn('danger', 'Удалить провайдера', 'trash'))
    if desktop:
        return inner
    return hdr_back('Anthropic API', 'API-ключ · sk-ant-…4f2a', href='Providers.dc.html', right=iconbtn('more', 'Действия с провайдером', 'ghost')) + scroll(inner)


page('ProviderModels.dc.html', 'Модели провайдера', [(k, t, pmodels(k)) for k, t in [('data', 'данные'), ('empty', 'пусто'), ('load', 'загрузка'), ('err', 'ошибка')]])


def providers_d(v):
    right = btn('primary', 'Добавить провайдера', 'plus')
    if v == 'empty':
        return desk('providers', 'Провайдеры', 'Модели для ботов', right, empty('plug', 'Провайдеров нет', 'Боты не смогут отвечать, пока не подключена хотя бы одна модель: по API-ключу, через свой endpoint или по подписке.', btn('primary', 'Добавить провайдера', 'plus')))
    if v == 'load':
        return desk('providers', 'Провайдеры', 'Модели для ботов', right, loading('Проверяю провайдеров', 5, 68), loading('Запрашиваю список моделей', 4, 60), 520)
    if v == 'err':
        return desk('providers', 'Провайдеры', 'Модели для ботов', right, error('Провайдеры не загрузились', 'Сервер не ответил за 10 с. Боты продолжают работать на прежних моделях.', btn('secondary', 'Повторить', 'refresh', go='data')))
    lst = banner('attention', 'risk', '2 бота без модели', 'Кодер и Архив ждут: их провайдеры недоступны.') + ''.join(prov_card(*p, sel=(i == 1)) for i, p in enumerate(PROV))
    aside = f'<div style="display: flex; align-items: center; gap: 10px;">{tile("key", "claude")}{two("Anthropic API", "API-ключ · sk-ant-…4f2a")}{iconbtn("more", "Действия с провайдером", "ghost")}</div>' + pmodels('data', True)
    return desk('providers', 'Провайдеры', 'Модели для ботов · 5 подключено', right, lst, aside, 520)


page('ProvidersDesktop.dc.html', 'Провайдеры на Mac', [(k, t, providers_d(k)) for k, t in [('data', 'данные'), ('empty', 'пусто'), ('load', 'загрузка'), ('err', 'ошибка')]], DW, DH)


def botmodel(v):
    base = hdr_back('Мак', 'Настройки бота', href='BotSettings.dc.html', av=avatar('claude', 'М', 36)) + scroll(
        card(f'<span style="{CALLB}">Модель</span><button type="button" style="min-height: 44px; display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 0 14px; border-radius: 12px; border: 1px solid var(--border-control); background: var(--bg-surface); color: var(--fg-default); font: 500 15px/20px {SANS};"><span style="display: flex; align-items: center; gap: 8px;">Sonnet 5{badge("claude", "Claude Code")}</span>{ic("down", 16, 2.4)}</button>')
        + card(f'<span style="{CALLB}">Где работает</span>' + seg(['Сервер', 'Mac'], 1, 'Где работает')))

    def grp(b, note, items):
        return f'<div style="display: flex; flex-direction: column; gap: 2px;"><div style="display: flex; align-items: center; gap: 8px;">{b}<span style="{FN}">{note}</span></div>{items}</div>'
    if v == 'empty':
        inner = empty('plug', 'Нет включённых моделей', 'Подключите провайдера и включите хотя бы одну модель.', btn('primary', 'Открыть провайдеров', href='Providers.dc.html'))
    elif v == 'load':
        inner = loading('Загружаю модели', 3, 48)
    elif v == 'err':
        inner = error('Модели не загрузились', 'Модель бота не изменена: Sonnet 5, Claude Code.', btn('secondary', 'Повторить', 'refresh', go='data'))
    else:
        inner = (grp(badge('claude', 'Claude Code'), 'подписка, расход в квоту', radio(False, 'Opus 5.5') + radio(True, 'Sonnet 5', 'сейчас у бота'))
                 + grp(badge('claude', 'Anthropic API'), 'ключ, оплата по токенам', radio(False, 'Opus 5.5'))
                 + grp(badge('', 'Ollama дома'), 'свой endpoint', radio(False, 'llama3.3', 'Провайдер не отвечает 3 ч', disabled=True))
                 + btn('primary', 'Готово', extra=FULL))
    return base + sheet('Модель бота', inner, sub='Мак · действует со следующего сообщения', av=avatar('claude', 'М', 36))


page('BotModel.dc.html', 'Выбор модели бота', [(k, t, botmodel(k)) for k, t in [('data', 'данные'), ('empty', 'пусто'), ('load', 'загрузка'), ('err', 'ошибка')]])

# ---------- 3. Терминал входа по подписке ----------
URL = 'https://claude.ai/oauth/authorize?code=true&amp;client_id=9d1c25&amp;scope=user:inference'
T_WAIT = [('$ claude login', 'm'), ("Browser didn't open? Use the url below to sign in:", ''), (URL, 'link'), ('', ''), ('Paste code here if prompted &gt;', '')]
T_OK = T_WAIT[:3] + [('Paste code here if prompted &gt; ****', ''), ('Login successful.', 'ok'), ('$', 'm')]
T_ERR = T_WAIT[:3] + [('Paste code here if prompted &gt; ****', ''), ('OAuth error: invalid_grant', 'err'), ('Press Enter to retry.', '')]
LINK = card(f'<div style="display: flex; align-items: center; gap: 10px;">{two("Ссылка для входа", "claude.ai/oauth/authorize?code=true…", mono=True)}{iconbtn("copy", "Скопировать ссылку")}{btn("primary", "Открыть", "ext", label="Открыть ссылку в браузере")}</div>', '10px 10px 10px 14px')


def code_row(go='ok'):
    i = uid()
    return f'<div style="display: flex; gap: 8px; align-items: center;"><label for="{i}" style="{SRONLY}">Код из браузера</label><input id="{i}" type="text" placeholder="Код из браузера" autocomplete="off" style="flex-grow: 1; min-width: 0; height: 44px; box-sizing: border-box; padding: 0 14px; border-radius: 12px; border: 1px solid var(--border-control); background: var(--bg-sunken); color: var(--fg-default); font: 400 15px/20px {MONO};">{iconbtn("paste", "Вставить код из буфера")}{btn("primary", "Отправить", go=go)}</div>'


def cli(v):
    h = hdr_back('Вход: Claude Code', 'Подписка Claude · терминал на сервере', href='Providers.dc.html', back_icon='x', back_label='Закрыть терминал', right=iconbtn('kbd', 'Клавиатура терминала', 'ghost', go='s3'))
    tw = 'padding: 0 8px; flex-grow: 1; min-height: 0; display: flex; flex-direction: column;'
    top = lambda b: f'<div style="padding: 12px 16px; flex-shrink: 0;">{b}</div>'
    low = lambda b: f'<div style="padding: 12px 16px; flex-shrink: 0; display: flex; flex-direction: column; gap: 10px;">{b}</div>'
    if v == 'load':
        return h + top(banner('neutral', 'spin', 'Шаг 1 из 3. Запускаю терминал', 'Готовлю claude login на сервере.')) + f'<div style="{tw}">{term([("Подключение", "m")], grow=True, cursor=False)}</div>' + bottom(btn('secondary', 'Отмена', href='Providers.dc.html'))
    if v == 's3':
        keys = [('Esc', ''), ('Tab', ''), ('Ctrl', ''), ('↑', 'Стрелка вверх'), ('↓', 'Стрелка вниз'), (ic('paste', 16) + 'Вставить', '')]
        return h + top(banner('attention', 'hand', 'Шаг 3 из 3. Вставьте код из браузера')) + f'<div style="{tw}">{term(T_WAIT, grow=True)}</div>' + f'<div style="padding: 8px 16px; flex-shrink: 0;">{code_row()}</div>' + keybar(keys) + oskbd()
    if v == 'ok':
        return (h + top(banner('success', 'check', 'Вход выполнен: 3 шага из 3', 'Claude Code подключён. Подписка: Max.')) + f'<div style="{tw}">{term(T_OK, grow=True)}</div>'
                + low(f'<span style="{FN}">Найдено моделей: 3. Терминал можно закрыть, вход сохранён на сервере.</span>') + bottom(btn('primary', 'Выбрать модели', href='ProviderModels.dc.html')))
    if v == 'err':
        return (h + top(banner('danger', 'risk', 'Шаг 3 из 3 не пройден: код не подошёл', 'Код действует 10 минут и только один раз.')) + f'<div style="{tw}">{term(T_ERR, grow=True)}</div>'
                + low('') + bottom(btn('secondary', 'Закрыть', href='Providers.dc.html') + btn('primary', 'Начать заново', 'refresh', go='s2'), 'repeat(2, minmax(0, 1fr))'))
    if v == 'lost':
        return (h + top(banner('danger', 'cloudoff', 'Связь с терминалом потеряна', 'Вход не завершён. Сессия на сервере живёт ещё 5 мин.')) + f'<div style="{tw} opacity: 0.55;">{term(T_WAIT, grow=True, cursor=False)}</div>'
                + low('') + bottom(btn('primary', 'Подключиться снова', 'refresh', go='s2')))
    return (h + top(banner('attention', 'hand', 'Шаг 2 из 3. Откройте ссылку и войдите', 'Сайт покажет код. Шаг 3: вставить его в поле внизу.')) + f'<div style="{tw}">{term(T_WAIT, grow=True)}</div>' + low(LINK) + bottom(code_row()))


page('CliLogin.dc.html', 'Терминал входа по подписке', [(k, t, cli(k)) for k, t in [('s2', 'шаг 2: ссылка'), ('s3', 'шаг 3: код и клавиатура'), ('ok', 'вход выполнен'), ('err', 'ошибка'), ('load', 'загрузка'), ('lost', 'связь потеряна')]])


def cli_d(v):
    st = {'wait': ['done', 'run', 'todo'], 'ok': ['done', 'done', 'done'], 'err': ['done', 'done', 'fail'], 'load': ['run', 'todo', 'todo']}[v]
    lines = {'wait': T_WAIT, 'ok': T_OK, 'err': T_ERR, 'load': [('Подключение', 'm')]}[v]
    ban = {'wait': banner('attention', 'hand', 'Шаг 2 из 3. Откройте ссылку и войдите', 'Сайт покажет код. Шаг 3: вставить его ниже.'), 'ok': banner('success', 'check', 'Вход выполнен', 'Claude Code подключён. Подписка: Max.'),
           'err': banner('danger', 'risk', 'Код не подошёл', 'Код действует 10 минут и только один раз.'), 'load': banner('neutral', 'spin', 'Запускаю терминал', 'Готовлю claude login на сервере.')}[v]
    acts = {'wait': btn('primary', 'Открыть ссылку в браузере', 'ext') + btn('secondary', 'Скопировать ссылку', 'copy') + lab('Код из браузера', code_row()),
            'ok': btn('primary', 'Выбрать модели', href='ProvidersDesktop.dc.html'), 'err': btn('primary', 'Начать заново', 'refresh', go='wait'), 'load': ''}[v]
    aside = (f'<h2 style="margin: 0; {HEAD}">Что происходит</h2>' + ban + steps([step(1, st[0], 'Запуск claude login'), step(2, st[1], 'Открыть ссылку и войти в аккаунт'), step(3, st[2], 'Вставить код из браузера')]) + acts
             + f'<span style="{FN} margin-top: auto;">Данные входа хранятся отдельно для каждого пользователя и доступны только его ботам.</span>')
    return desk('providers', 'Вход: Claude Code', 'Подписка Claude · терминал на сервере', btn('secondary', 'Закрыть', 'x', href='ProvidersDesktop.dc.html'), term(lines, grow=True, fs=13, cursor=v != 'load'), aside, 400)


page('CliLoginDesktop.dc.html', 'Терминал входа на Mac', [(k, t, cli_d(k)) for k, t in [('wait', 'ожидание кода'), ('ok', 'вход выполнен'), ('err', 'ошибка'), ('load', 'загрузка')]], DW, DH)
