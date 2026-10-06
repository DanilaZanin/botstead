"""Экраны этапа 2, часть Б: браузер бота, запись и процедуры, выбор аватара, память, активность, разрешения."""
# ruff: noqa: F403,F405,E731
from screens2_lib import *  # noqa: F401,F403
from screens2_a import grid, lab

SCOUT = avatar('gemini', 'С', 36)
TECH_SUM = f'min-height: 44px; display: flex; align-items: center; {FN} cursor: pointer;'


def tech(lines, label='Технические подробности'):
    """Имена инструментов и аргументы прячутся под раскрытие: в основном тексте только человеческие названия."""
    body = ''.join(f'<span>{l}</span>' for l in lines)
    return f'<details style="flex-shrink: 0;"><summary style="{TECH_SUM}">{label}</summary><div style="display: flex; flex-direction: column; gap: 2px; font: 400 12px/16px {MONO}; color: var(--fg-muted);">{body}</div></details>'


# ---------- 4. Браузер бота ----------
B_URL = 'jobs.example.eu/apply/812'
CTL = {'bot': banner('neutral', 'bot', 'Управляет бот', 'Шаг 4 из 6: заполняет форму отклика.'),
       'human': banner('attention', 'hand', 'Управляете вы', 'Бот остановлен, ожидавшие действия отменены. Касание работает как клик.'),
       'ret': banner('neutral', 'spin', 'Возврат боту', 'Бот заново читает страницу. Прежние подтверждения сброшены.'),
       'ask': banner('attention', 'shield', 'Бот ждёт решения', 'Действие не выполнено, страница не тронута.'),
       'pwd': banner('attention', 'key', 'Бот ждёт пароль', 'Поле «Пароль» на странице входа.')}
SEE = {'bot': ('eye', 'Бот видит экран'), 'human': ('eyeoff', 'Бот не видит экран'), 'ret': ('eye', 'Бот снова видит экран'), 'ask': ('eye', 'Бот видит экран'), 'pwd': ('eyeoff', 'Бот не видит экран и ввод')}
B_TECH = ['09:52:10 browser.navigate jobs.example.eu/apply/812', '09:52:12 browser.snapshot', '09:52:14 browser.click button «Откликнуться»', '09:52:15 browser.fill textbox «Сопроводительное письмо»']


def bsteps(c):
    s4 = step(4, 'pause', 'Заполнение формы отклика', extra=f'<span style="{FN}">На паузе, пока управляете вы</span>') if c in ('human', 'pwdh') else step(4, 'run', 'Заполняет форму отклика')
    return [step(1, 'done', 'Открыл страницу вакансии'), step(2, 'done', 'Прочитал требования'), step(3, 'done', 'Нажал «Откликнуться»'), s4, step(5, 'todo', 'Приложить резюме EN'),
            step(6, 'todo', 'Отправить отклик', extra=f'<div>{tag("подтверждение", "attention", "shield")}</div>')]


def seeline(c, pad='6px 16px'):
    icn, txt = SEE[c]
    return (f'<div role="status" style="flex-shrink: 0; display: flex; align-items: center; gap: 8px; min-height: 32px; box-sizing: border-box; padding: {pad}; background: var(--bg-surface); border-bottom: 1px solid var(--border-default); font: 400 13px/18px {SANS}; color: var(--fg-default);">'
            f'<span style="color: var(--fg-muted); display: flex;">{ic(icn, 16)}</span><span style="flex-grow: 1;">{txt}</span><span aria-label="Расход задачи" style="font: 400 12px/16px {MONO}; color: var(--fg-muted);">18k токенов · 02:14</span></div>')


def approval_card(no='bot', yes='bot'):
    dl = ''.join(f'<dt style="{FN} color: var(--attention-text);">{a}</dt><dd style="margin: 0; {CALL} color: var(--fg-default);">{b}</dd>' for a, b in [('Куда', 'jobs.example.eu/apply/812'), ('Данные', 'Резюме EN, email'), ('Отменить', 'Нельзя')])
    return (f'<section role="alertdialog" aria-label="Подтверждение действия" style="padding: 14px; border-radius: 16px; background: var(--attention-bg); border: 1px solid var(--attention-border); box-shadow: var(--shadow-sheet); display: flex; flex-direction: column; gap: 10px; flex-shrink: 0;">'
            f'<div style="display: flex; align-items: center; gap: 6px; color: var(--attention-text); {CAP}">{ic("risk", 16, 2.2)}ОТПРАВКА</div><div style="font: 500 17px/22px {SANS}; color: var(--fg-default);">Отправить отклик на «Senior SRE, Belgrade/remote»</div>'
            f'<dl style="margin: 0; display: grid; grid-template-columns: auto 1fr; gap: 4px 14px; align-items: baseline;">{dl}</dl>' + grid(btn('secondary', 'Отклонить', go=no) + btn('approve', 'Разрешить', go=yes)) + '</section>')


def pwd_form(go='bot'):
    return (banner('neutral', 'lock', 'Модель пароль не увидит', 'Он уйдёт прямо в поле на странице. В ленту, запись и память не попадёт.')
            + field('Пароль', '', 'Пароль от jobs.example.eu', typ='password', right=iconbtn('eye', 'Показать пароль', 'ghost'))
            + card(switch(False, 'Сохранить в хранилище секретов', 'Бот сможет входить сам, значение ему не показывается'), '4px 14px')
            + grid(btn('secondary', 'Отмена', go=go) + btn('primary', 'Ввести на странице', go=go)))


RECREATE = f'<span style="{FN} max-width: 300px;">Пересоздание сохранит файлы, память и профиль браузера бота. Открытые вкладки и текущая задача потеряются.</span>'
B_ERR = ('Экран недоступен', 'Компьютер бота не отвечает. Задача остановлена на шаге 4, отклик не отправлен.')


def bbar(c):
    stop = btn('danger', 'Стоп', 'stop')
    if c == 'human':
        return bottom(btn('primary', 'Вернуть боту', 'play', go='ret') + iconbtn('kbd', 'Клавиатура') + stop, '1fr 44px auto')
    if c == 'ret':
        return bottom(btn('primary', 'Возвращаю управление', busy=True, disabled=True) + stop, '1fr auto')
    return bottom(btn('primary', 'Перехватить', 'hand', go='human') + stop, '1fr auto')


def browser(v):
    h = hdr_back('Браузер · Скаут', 'Свой браузер бота', href='Thread.dc.html', av=SCOUT, right=iconbtn('list', 'Все шаги', 'ghost', go='steps'))
    if v == 'empty':
        return h + empty('globe', 'Браузер ещё не открыт', 'Бот откроет его сам, когда задаче понадобится сайт. Профиль и cookies у каждого бота свои.', btn('secondary', 'Открыть браузер', 'globe', go='bot'))
    if v == 'err':
        return h + error(*B_ERR, btn('primary', 'Подключиться снова', 'refresh', go='bot') + btn('secondary', 'Пересоздать компьютер') + RECREATE)
    abar = lambda inner: f'<div style="flex-shrink: 0; padding: 8px 12px; display: flex; background: var(--bg-surface);">{inner}</div>'
    if v == 'load':
        return (h + abar('<div style="flex-grow: 1; height: 36px; border-radius: 10px; background: var(--bg-sunken);"></div>')
                + f'<div role="status" aria-busy="true" style="height: 246px; flex-shrink: 0; background: var(--bg-sunken); display: flex; align-items: center; justify-content: center; gap: 8px; {FN}">{spin(16)}Подключаюсь к экрану</div>' + scroll(loading('Загружаю шаги', 3, 56)))
    human = v in ('human', 'pwdh')
    c = 'human' if human else (v if v in CTL else 'bot')
    st = bsteps(c)
    hide = btn('secondary', 'Ввести пароль скрыто', 'key', go='pwdh') if human else ''
    under = scroll(CTL[c] + hide + section('Последние шаги') + steps(st[2:4] if human else st[1:4]))
    scr = live('100%', 246, frame='attention-border' if human else 'border-default', tools=True)
    see = seeline('pwd' if v in ('pwd', 'pwdh') else c)
    if v == 'ask':
        mid = f'<div style="position: relative; flex-grow: 1; min-height: 0; display: flex; flex-direction: column;">{scr}{under}<div style="position: absolute; left: 0; top: 0; width: 100%; height: 100%; box-sizing: border-box; padding: 16px; background: rgba(0, 0, 0, 0.35); display: flex; flex-direction: column;">{approval_card()}</div></div>'
    else:
        mid = scr + under
    out = h + abar(addr(B_URL, 'human' if human else 'bot')) + see + mid + bbar(c)
    if v == 'steps':
        out += sheet('Шаги', steps(st, 12) + tech(B_TECH), sub='Скаут · тред «Вакансии SRE» · 4 из 6', av=SCOUT, close='bot')
    if v == 'pwd':
        out += sheet('Пароль для сайта', pwd_form(), sub='jobs.example.eu · просит Скаут', av=SCOUT, close='bot')
    if v == 'pwdh':
        out += sheet('Пароль для сайта', pwd_form('human'), sub='jobs.example.eu · вводите вы', close='human')
    return out


BV = [('bot', 'управляет бот'), ('human', 'управляете вы'), ('pwdh', 'управляете вы: пароль'), ('ret', 'возврат боту'), ('steps', 'лента шагов'), ('ask', 'подтверждение'), ('pwd', 'бот просит пароль'), ('empty', 'пусто'), ('load', 'загрузка'), ('err', 'ошибка')]
page('Browser.dc.html', 'Браузер бота', [(k, t, browser(k)) for k, t in BV])


def browser_d(v):
    human = v in ('human', 'pwdh')
    c = 'human' if human else (v if v in CTL else 'bot')
    who = {'human': 'у вас', 'ret': 'возврат'}.get(c, 'работает')
    stop = btn('danger', 'Стоп', 'stop')
    right = {'human': btn('primary', 'Вернуть боту', 'play', go='ret'), 'ret': btn('primary', 'Возвращаю управление', busy=True, disabled=True)}.get(c, btn('primary', 'Перехватить', 'hand', go='human')) + stop
    sub = 'Свой браузер бота · тред «Вакансии SRE»'
    if v == 'empty':
        return desk('', 'Браузер · Скаут', sub, '', empty('globe', 'Браузер ещё не открыт', 'Бот откроет его сам, когда задаче понадобится сайт. Профиль и cookies у каждого бота свои.', btn('secondary', 'Открыть браузер', 'globe', go='bot')))
    if v == 'err':
        return desk('', 'Браузер · Скаут', sub, '', error(*B_ERR, btn('primary', 'Подключиться снова', 'refresh', go='bot') + btn('secondary', 'Пересоздать компьютер') + RECREATE), scout='ошибка')
    if v == 'load':
        body = f'<div role="status" aria-busy="true" style="flex-grow: 1; border-radius: 12px; background: var(--bg-sunken); display: flex; align-items: center; justify-content: center; gap: 8px; {FN}">{spin(16)}Подключаюсь к экрану</div>'
        return desk('', 'Браузер · Скаут', sub, '', body, loading('Загружаю шаги', 4, 56))
    extra = f'<div style="display: flex; align-items: center; gap: 12px;">{btn("secondary", "Ввести пароль скрыто", "key", go="pwdh")}<span style="{FN}">Мышь и клавиатура идут в браузер бота. Бот ничего не делает, пока управление не вернётся.</span></div>' if human else ''
    body = (f'<div style="display: flex;">{addr(B_URL, "human" if human else "bot")}</div>' + live('100%', 460, frame='attention-border' if human else 'border-default', tools=True)
            + f'<div style="border: 1px solid var(--border-default); border-radius: 12px; overflow: hidden; flex-shrink: 0;">{seeline("pwd" if v in ("pwd", "pwdh") else c, "6px 14px")}</div>' + CTL[c] + extra)
    aside = f'<h2 style="margin: 0; {HEAD}">Шаги</h2>' + (approval_card() if v == 'ask' else '') + steps(bsteps(c), 12) + tech(B_TECH)
    out = desk('', 'Браузер · Скаут', sub, right, body, aside, scout=who)
    if v == 'pwd':
        out += dialog('Пароль для сайта', pwd_form(), sub='jobs.example.eu · просит Скаут', close='bot')
    if v == 'pwdh':
        out += dialog('Пароль для сайта', pwd_form('human'), sub='jobs.example.eu · вводите вы', close='human')
    return out


page('BrowserDesktop.dc.html', 'Браузер бота на Mac', [(k, t, browser_d(k)) for k, t in BV if k != 'steps'], DW, DH)

# ---------- 5. Запись и процедуры ----------
RSTEPS = [('Открыть страницу', 'jobs.example.eu/login', ''), ('Ввод в поле «Email»', '', tag('параметр {почта}')), ('Ввод в поле «Пароль»', '', tag('секрет', 'neutral', 'lock')),
          ('Нажать «Войти»', '', ''), ('Ввод в поле «Город»', '', tag('параметр {город}')), ('Нажать «Найти»', '', ''),
          ('Ждать список вакансий', 'до 10 с', tag('можно повторять')), ('Нажать «Откликнуться»', '', tag('подтверждение', 'attention', 'shield'))]


def rstep(n, title, detail, tags, actions=''):
    d = f'<span style="font: 400 12px/16px {MONO}; color: var(--fg-muted);">{detail}</span>' if detail else ''
    meta = f'<span style="display: flex; flex-wrap: wrap; align-items: center; gap: 6px;">{d}{tags}</span>' if (detail or tags) else ''
    return (f'<span aria-hidden="true" style="width: 24px; height: 24px; flex-shrink: 0; border-radius: 12px; background: var(--bg-sunken); color: var(--fg-default); display: flex; align-items: center; justify-content: center; font: 600 12px/16px {MONO};">{n}</span>'
            f'<span style="flex-grow: 1; min-width: 0; display: flex; flex-direction: column; gap: 3px;"><span style="{CALL} color: var(--fg-default);">{title}</span>{meta}</span>{actions}')


def recbar(icon, title, sub, live_=True):
    return f'<div role="status" style="flex-shrink: 0; display: flex; align-items: center; gap: 10px; padding: 8px 16px; background: var(--bg-surface); border-bottom: 1px solid var(--border-default);"><span style="color: var(--{"danger-fg" if live_ else "fg-muted"}); display: flex;">{ic(icon, 22)}</span><span style="flex-grow: 1; display: flex; flex-direction: column;"><span style="{CALLB} color: var(--fg-default);">{title}</span><span style="font: 400 12px/16px {MONO}; color: var(--fg-muted);">{sub}</span></span></div>'


def rec(v):
    h = hdr_back('Запись · Скаут', 'Покажите действие один раз', href='Procedures.dc.html', av=SCOUT)
    abar = f'<div style="flex-shrink: 0; padding: 8px 12px; display: flex; background: var(--bg-surface); border-bottom: 1px solid var(--border-default);">{addr("jobs.example.eu/search", "human")}</div>'
    done = btn('primary', 'Завершить запись', 'check', href='RecordingReview.dc.html')
    if v == 'load':
        return h + recbar('rec', 'Готовлю запись', 'открываю браузер бота', False) + f'<div role="status" aria-busy="true" style="height: 246px; flex-shrink: 0; background: var(--bg-sunken); display: flex; align-items: center; justify-content: center; gap: 8px; {FN}">{spin(16)}Подключаюсь к экрану</div>' + scroll('')
    if v == 'err':
        return (h + abar + live('100%', 246) + error('Сайт запретил запись действий', 'Защита сайта не дала встроить запись в страницу. Ручные действия здесь не записать, но процедуру можно собрать из шагов бота.', btn('primary', 'Записать по шагам бота', 'bot') + btn('secondary', 'Закрыть', href='Procedures.dc.html')))
    lst = '<ol style="list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 10px;">' + ''.join(f'<li style="display: flex; gap: 10px; align-items: flex-start;">{rstep(i + 1, *RSTEPS[i])}</li>' for i in range(1, 6)) + '</ol>'
    if v == 'empty':
        return h + recbar('rec', 'Идёт запись', '00:03 из 10:00 · шагов нет') + abar + live('100%', 246, frame='danger-fg', tools=True) + empty('cursor', 'Шагов пока нет', 'Действия на странице появятся здесь: переходы, клики, ввод.') + bottom(btn('secondary', 'Пауза', 'pause', go='paused') + done, '1fr 1.6fr')
    if v == 'paused':
        return h + recbar('pause', 'Запись на паузе', '02:14 · действия не записываются', False) + abar + live('100%', 246, frame='border-control', tools=True) + scroll(section('Записанные шаги · 6') + lst) + bottom(btn('secondary', 'Возобновить', 'rec', go='on') + done, '1fr 1.6fr')
    return h + recbar('rec', 'Идёт запись', '02:14 из 10:00 · 6 шагов') + abar + live('100%', 246, frame='danger-fg', tools=True) + scroll(section('Записанные шаги · 6') + lst) + bottom(btn('secondary', 'Пауза', 'pause', go='paused') + done, '1fr 1.6fr')


page('Recording.dc.html', 'Запись процедуры', [(k, t, rec(k)) for k, t in [('on', 'запись идёт'), ('paused', 'пауза'), ('empty', 'пусто'), ('load', 'загрузка'), ('err', 'ошибка')]])


def review(v):
    h = hdr_back('Разбор записи', '8 шагов · 02:31 · jobs.example.eu', href='Recording.dc.html')
    if v == 'empty':
        return h + empty('cursor', 'В записи нет шагов', 'За 00:12 на странице не было действий. Запись можно повторить.', btn('primary', 'Записать заново', 'rec', href='Recording.dc.html'))
    if v == 'load':
        return h + scroll(loading('Разбираю запись на шаги', 5, 56))
    if v == 'err':
        return h + error('Запись не разобрана', 'Сырая запись сохранена. Разбор можно повторить или записать действие заново.', btn('primary', 'Повторить разбор', 'refresh', go='data') + btn('secondary', 'Записать заново', 'rec', href='Recording.dc.html'))
    lst = rows([rstep(i + 1, *s, actions=iconbtn('edit', f'Изменить шаг {i + 1}', 'muted', go='edit') + iconbtn('trash', f'Удалить шаг {i + 1}', 'muted')) for i, s in enumerate(RSTEPS)], 5)
    out = h + scroll(f'<span style="{FN}">Найдено 2 параметра и 1 секрет. Значение пароля в запись не попало.</span>' + lst, 'gap: 10px; padding: 12px 16px;') + bottom(btn('ghost', 'Удалить запись', 'trash') + btn('primary', 'Дальше: параметры', href='ProcedureEdit.dc.html'), '1fr 1.3fr')
    if v == 'edit':
        out += sheet('Шаг 5: ввод', field('Название', 'Ввод в поле «Город»') + lab('Что вводить', seg(['Текст', 'Параметр', 'Секрет'], 1, 'Что вводить'))
                     + grid(field('Имя параметра', 'город', mono=True) + field('По умолчанию', 'Belgrade'))
                     + card(switch(False, 'Спрашивать подтверждение') + switch(True, 'Можно повторять', 'Второй запуск шага ничего не сломает'), '4px 14px', 0)
                     + grid(btn('danger', 'Удалить шаг', 'trash', go='data') + btn('primary', 'Сохранить', go='data')), sub='Куда: поле «Город» на странице поиска', close='data')
    return out


page('RecordingReview.dc.html', 'Разбор записи на шаги', [(k, t, review(k)) for k, t in [('data', 'шаги'), ('edit', 'правка шага'), ('empty', 'пусто'), ('load', 'загрузка'), ('err', 'ошибка')]])

PARAMS = [('type', 'почта', 'Текст · обязательный'), ('type', 'город', 'Текст · по умолчанию Belgrade'), ('lock', 'пароль', 'Секрет из хранилища')]


def pedit(v):
    h = hdr_back('Новая процедура', 'Имя и параметры', href='RecordingReview.dc.html')
    busy, e = v == 'load', v == 'err'
    top = banner('danger', 'risk', 'Не сохранено. Запись и шаги на месте.') if e else ''
    name = field('Название', 'Отклик на вакансию', err='Процедура с таким именем уже есть' if e else '', disabled=busy)
    bot = lab('Бот', f'<button type="button" style="min-height: 44px; display: flex; align-items: center; gap: 10px; padding: 0 14px; border-radius: 12px; border: 1px solid var(--border-control); background: var(--bg-surface); color: var(--fg-default); font: 500 15px/20px {SANS};">{avatar("gemini", "С", 28)}<span style="flex-grow: 1; text-align: left;">Скаут</span>{ic("down", 16, 2.4)}</button>')
    if v == 'empty':
        par = section('Параметры') + card(f'<span style="{FN}">Параметров нет. Процедура запускается одним нажатием и всегда делает одно и то же.</span>' + btn('secondary', 'Добавить параметр', 'plus'))
    else:
        par = section('Параметры · 3', btn('ghost', 'Добавить', 'plus', extra='padding: 0 8px;')) + rows([tile(i) + two(n, s) + iconbtn('edit', f'Изменить параметр {n}', 'muted') for i, n, s in PARAMS])
    chk = section('Проверка результата') + field('Страница содержит', 'Отклик отправлен', hint='' if e else 'Без проверки процедура считается выполненной после последнего шага.', disabled=busy)
    b = btn('primary', 'Сохраняю', busy=True, disabled=True) if busy else btn('primary', 'Сохранить процедуру', href='Procedures.dc.html')
    return h + scroll(top + name + bot + par + chk, 'gap: 10px;') + bottom(b)


page('ProcedureEdit.dc.html', 'Параметры процедуры', [(k, t, pedit(k)) for k, t in [('data', 'данные'), ('empty', 'пусто'), ('load', 'загрузка'), ('err', 'ошибка')]])

PROCS = [('Отклик на вакансию', '8 шагов · 3 параметра · Скаут', status('success-fg', 'вчера: готово за 41 с'), 'ProcedureRun.dc.html'), ('Перевыпуск сертификата', '4 шага · 2 параметра · SRE', status('success-fg', '21 сен: готово за 2 мин'), 'ProcedureRun.dc.html'),
         ('Выгрузка счёта', '6 шагов · 1 параметр · Мак', status('danger-fg', '2 окт: сбой на шаге 5', 'x'), 'Replay.dc.html'), ('Скачать выписку', '5 шагов · без параметров · Скаут', status('neutral-fg', 'ещё не запускалась'), 'ProcedureRun.dc.html')]


def rtabs(active):
    """Внутри вкладки «Рутины» два раздела: расписания и процедуры."""
    cells = ''
    for i, (t, hrf) in enumerate([('Расписания', 'Schedules.dc.html'), ('Процедуры', 'Procedures.dc.html')]):
        a = i == active
        cur = ' aria-current="page"' if a else ''
        cells += f'<a href="{hrf}"{cur} style="min-height: 44px; display: flex; align-items: center; justify-content: center; border-radius: 9px; border: 1px solid {"var(--border-default)" if a else "transparent"}; background: {"var(--bg-surface)" if a else "transparent"}; color: var(--fg-default); text-decoration: none; font: {"600" if a else "500"} 14px/18px {SANS};">{t}</a>'
    return f'<nav aria-label="Разделы рутин" style="flex-shrink: 0; margin: 0 16px 8px; display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 4px; padding: 4px; border-radius: 12px; background: var(--bg-sunken);">{cells}</nav>'


def proc_card(n, s, st, href, sel=False):
    return f'<div style="display: flex; align-items: center; gap: 12px; padding: 12px 14px; {CARD} {"border-color: var(--fg-default);" if sel else ""} flex-shrink: 0;">{tile("list")}<a href="Replay.dc.html" style="flex-grow: 1; min-width: 0; min-height: 44px; display: flex; text-decoration: none; color: var(--fg-default);">{two(n, s, st)}</a>{iconbtn("play", f"Запустить: {n}", "secondary", href)}</div>'


def procs(v):
    h = hdr_root('Рутины') + rtabs(1)
    add = actionbar(btn('primary', 'Записать процедуру', 'rec', href='Recording.dc.html')) + tabbar(1)
    if v == 'empty':
        return h + empty('rec', 'Процедур нет', 'Процедура это записанные действия в браузере бота. Она повторяется по шагам, без модели и без расхода.') + add
    if v == 'load':
        return h + scroll(loading('Загружаю процедуры', 4, 84), 'padding-top: 4px;') + add
    if v == 'err':
        return h + error('Процедуры не загрузились', 'Сервер ответил 500. Расписания с процедурами продолжают работать.', btn('secondary', 'Повторить', 'refresh', go='data')) + add
    return h + scroll(''.join(proc_card(*p) for p in PROCS), 'padding-top: 4px; gap: 10px;') + add


page('Procedures.dc.html', 'Рутины: процедуры', [(k, t, procs(k)) for k, t in [('data', 'данные'), ('empty', 'пусто'), ('load', 'загрузка'), ('err', 'ошибка')]])


def prun(v):
    base = procs('data')
    cancel = btn('secondary', 'Отмена', href='Procedures.dc.html')
    secret = f'<div style="display: flex; align-items: center; gap: 12px; min-height: 44px;"><span style="color: var(--fg-muted); display: flex;">{ic("lock", 20)}</span>{two("Пароль", "из хранилища секретов · jobs.example.eu")}</div>'
    if v == 'none':
        inner = f'<span style="{CALL} color: var(--fg-default);">Параметров нет. 5 шагов, подтверждений не будет.</span>' + grid(cancel + btn('primary', 'Запустить', 'play', href='Replay.dc.html'))
        return base + sheet('Запуск процедуры', inner, sub='Скачать выписку · Скаут', av=SCOUT)
    if v == 'err':
        inner = banner('danger', 'cloudoff', 'Компьютер бота недоступен', 'Компьютер Скаута не отвечает. Процедура не запущена.') + grid(btn('secondary', 'Закрыть', href='Procedures.dc.html') + btn('primary', 'Повторить', 'refresh', go='data'))
        return base + sheet('Запуск процедуры', inner, sub='Отклик на вакансию · Скаут', av=SCOUT)
    bad, busy = v == 'bad', v == 'busy'
    go_btn = btn('primary', 'Запускаю', busy=True, disabled=True) if busy else btn('primary', 'Запустить', 'play', href='Replay.dc.html')
    inner = (field('Почта', '' if bad else 'me@example.org', err='Обязательный параметр' if bad else '', disabled=busy) + field('Город', 'Belgrade', hint='Значение по умолчанию', disabled=busy) + secret
             + card(switch(False, 'Пробный прогон', 'Дойти до отправки и остановиться'), '4px 14px') + f'<span style="{FN}">1 шаг попросит подтверждение: «Откликнуться».</span>' + grid(cancel + go_btn))
    return base + sheet('Запуск процедуры', inner, sub='Отклик на вакансию · Скаут', av=SCOUT)


page('ProcedureRun.dc.html', 'Запуск процедуры с параметрами', [(k, t, prun(k)) for k, t in [('data', 'параметры'), ('none', 'пусто'), ('busy', 'загрузка'), ('bad', 'ошибка ввода'), ('err', 'ошибка')]])

TXT13 = f'font: 400 13px/18px {SANS}; color: var(--fg-default);'


SNAP = '<div style="width: 62%; display: flex; flex-direction: column; gap: 8px;"><span style="width: 70%; height: 8px; border-radius: 4px; background: var(--border-control);"></span><span style="height: 6px; border-radius: 3px; background: var(--border-default);"></span><span style="height: 20px; border-radius: 6px; border: 1px solid var(--border-control); background: var(--bg-surface);"></span></div>'


def r_fail(snap_h=84):
    snap = f'<figure style="margin: 0; display: flex; flex-direction: column; gap: 4px;">{live("100%", snap_h, SNAP, frame="danger-fg", note="Снимок страницы в момент сбоя")}<figcaption style="{TXT13}">Страница в момент сбоя · 09:52:41</figcaption></figure>' if snap_h else ''
    return (snap +
            f'<span style="{TXT13}">Шаги 1–4 выполнены. Отклик не отправлен.</span>'
            + grid(btn('primary', 'Исправить шаг', href='RecordingReview.dc.html') + btn('secondary', 'Повторить', go='run', label='Повторить шаг'))
            + grid(btn('secondary', 'Передать боту') + btn('secondary', 'Показать самому', href='Browser.dc.html'))
            + f'<span style="{TXT13}">«Передать боту»: Скаут продолжит сам, с этого места начнут тратиться токены.</span>')


def rsteps_for(v, snap_h=84):
    n = {'run': 5, 'fail': 5, 'ask': 8, 'done': 9}[v]
    out = []
    for i, (t, d, _) in enumerate(RSTEPS, 1):
        if i < n:
            out.append(step(i, 'done', t))
        elif i == n and v == 'run':
            out.append(step(i, 'run', t, d))
        elif i == n and v == 'fail':
            out.append(step(i, 'fail', t, extra=f'<span style="{TXT13}">Не найдено поле «Город» на странице.</span>' + r_fail(snap_h), tone='fail'))
        elif i == n and v == 'ask':
            out.append(step(i, 'wait', t))
        elif v == 'fail':
            if i == n + 1:
                out.append(step('6–8', 'todo', 'Шаги 6–8 не выполнялись'))
        else:
            out.append(step(i, 'todo', t))
    return steps(out, 8)


def rprog(v):
    p, txt, col = {'run': (50, 'Шаг 5 из 8 · 00:27', 'fg-default'), 'fail': (50, 'Остановлено на шаге 5 из 8', 'danger-fg'), 'ask': (88, 'Шаг 8 из 8 · ждёт решения', 'attention-fg'), 'done': (100, 'Готово · 8 из 8', 'success-fg')}[v]
    return card(f'<div style="display: flex; justify-content: space-between; gap: 8px;"><span style="{CALLB}">{txt}</span><span style="font: 400 12px/20px {MONO}; color: var(--fg-muted);">0 токенов</span></div><div role="progressbar" aria-label="Ход процедуры" aria-valuenow="{p}" aria-valuemin="0" aria-valuemax="100" style="height: 8px; border-radius: 4px; background: var(--bg-sunken); overflow: hidden;"><div style="width: {p}%; height: 8px; border-radius: 4px; background: var(--{col});"></div></div>', '12px 14px', 8)


def replay(v):
    h = hdr_back('Отклик на вакансию', 'Без модели · Скаут', href='Procedures.dc.html', av=SCOUT, right=iconbtn('screen', 'Экран бота', 'secondary', 'Browser.dc.html'))
    if v == 'load':
        return h + scroll(loading('Запускаю процедуру', 4, 56))
    if v == 'err':
        return h + error('Связь с ботом потеряна', 'Процедура остановлена на шаге 5 из 8. Повторный запуск продолжит с этого шага, а не сначала.', btn('primary', 'Продолжить с шага 5', 'play', go='run') + btn('secondary', 'К процедурам', href='Procedures.dc.html'))
    done = banner('success', 'check', 'Готово за 41 с', 'Проверка пройдена: страница содержит «Отклик отправлен».') if v == 'done' else ''
    ask = approval_card('fail', 'done') if v == 'ask' else ''
    if v == 'done':
        b = bottom(btn('secondary', 'К процедурам', href='Procedures.dc.html') + btn('primary', 'Запустить ещё раз', 'play', go='run'), 'repeat(2, minmax(0, 1fr))')
    elif v == 'fail':
        b = bottom(btn('secondary', 'К процедурам', href='Procedures.dc.html'))
    else:
        b = bottom(btn('danger', 'Стоп', 'stop'))
    return h + scroll(rprog(v) + done + card(rsteps_for(v), '12px 14px') + ask, 'gap: 10px; padding: 12px 16px;') + b


RV = [('run', 'идёт'), ('fail', 'сбой на шаге'), ('ask', 'подтверждение'), ('done', 'готово'), ('load', 'загрузка'), ('err', 'ошибка')]
page('Replay.dc.html', 'Ход воспроизведения', [(k, t, replay(k)) for k, t in RV])


def procs_d(v):
    right = btn('primary', 'Записать процедуру', 'rec')
    sub = 'Записанные действия, идут без модели'
    if v == 'empty':
        return desk('routines', 'Процедуры', sub, right, empty('rec', 'Процедур нет', 'Процедура это записанные действия в браузере бота. Она повторяется по шагам, без модели и без расхода.', btn('primary', 'Записать процедуру', 'rec')))
    if v == 'load':
        return desk('routines', 'Процедуры', sub, right, loading('Загружаю процедуры', 4, 76), loading('Подключаюсь к экрану', 2, 120), 420)
    if v == 'err':
        return desk('routines', 'Процедуры', sub, right, error('Процедуры не загрузились', 'Сервер ответил 500. Расписания с процедурами продолжают работать.', btn('secondary', 'Повторить', 'refresh', go='run')))
    lst = f'<div style="width: 300px; flex-shrink: 0; display: flex; flex-direction: column; gap: 10px;">{"".join(proc_card(*p, sel=(i == 0)) for i, p in enumerate(PROCS))}</div>'
    done = banner('success', 'check', 'Готово за 41 с', 'Проверка пройдена: страница содержит «Отклик отправлен».') if v == 'done' else ''
    act = {'done': btn('primary', 'Запустить ещё раз', 'play', go='run'), 'fail': ''}.get(v, btn('danger', 'Стоп', 'stop'))
    det = f'<div style="flex-grow: 1; min-width: 0; display: flex; flex-direction: column; gap: 10px;"><div style="display: flex; align-items: center; gap: 10px;">{SCOUT}{two("Отклик на вакансию", "Без модели · Скаут")}{act}</div>{rprog(v)}{done}{card(rsteps_for(v, 0) if v == "fail" else rsteps_for(v), "12px 14px")}</div>'
    stat = {'run': banner('neutral', 'play', 'Идёт процедура', 'Шаги идут без модели, токены не тратятся.'), 'fail': banner('danger', 'risk', 'Остановлено на шаге 5 из 8', 'На экране страница в момент сбоя. Отклик не отправлен.'),
            'done': banner('success', 'check', 'Процедура завершена', 'На экране итоговая страница.')}[v]
    aside = (f'<h2 style="margin: 0; {HEAD}">Экран бота</h2><div style="display: flex;">{addr("jobs.example.eu/search")}</div>' + live('100%', 212, frame='danger-fg' if v == 'fail' else 'border-default', tools=True) + stat
             + btn('secondary', 'Перехватить', 'hand'))
    return desk('routines', 'Процедуры', sub + ' · 4', right, f'<div style="display: flex; gap: 16px; align-items: flex-start;">{lst}{det}</div>', aside, 372, scout='стоп' if v == 'fail' else ('готово' if v == 'done' else 'работает'))


page('ProceduresDesktop.dc.html', 'Процедуры на Mac', [(k, t, procs_d(k)) for k, t in [('run', 'идёт'), ('fail', 'сбой на шаге'), ('done', 'готово'), ('empty', 'пусто'), ('load', 'загрузка'), ('err', 'ошибка')]], DW, DH)

# ---------- 6. Выбор аватара (шаг 2 мастера нового бота) ----------
def avatar_pick(v):
    h = hdr_back('Новый бот', 'Шаг 2 из 3: персонаж', href='NewBotDescribe.dc.html')
    if v == 'err':
        return h + error('Персонажи не загрузились', 'Бот получит Робота. Персонажа можно сменить позже в настройках.', btn('primary', 'Повторить', 'refresh', go='rand') + btn('secondary', 'Продолжить с Роботом', href='NewBotConfirm.dc.html'))
    tiles = lambda sel: '<div role="group" aria-label="Все персонажи" style="display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 8px;">' + ''.join(
        f'<button type="button" aria-label="Персонаж: {n}" aria-pressed="{"true" if i == sel else "false"}" onClick="{H("go_picked")}" style="height: 66px; padding: 0; border-radius: 16px; border: 2px solid {"var(--fg-default)" if i == sel else "transparent"}; background: transparent; display: flex; align-items: center; justify-content: center;">{svg}</button>' for i, (n, svg) in enumerate(all15(54))) + '</div>'
    if v == 'load':
        sk = '<div style="display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 8px;">' + '<span style="height: 66px; border-radius: 16px; background: var(--bg-sunken);"></span>' * 15 + '</div>'
        return h + scroll(f'<div role="status" aria-busy="true" style="display: flex; flex-direction: column; align-items: center; gap: 10px; padding: 8px 0;"><span style="width: 128px; height: 128px; border-radius: 32px; background: var(--bg-sunken);"></span><span style="display: flex; align-items: center; gap: 8px; {FN}">{spin(14)}Подбираю персонажа</span></div>' + section('Все персонажи · 15') + sk) + bottom(btn('primary', 'Дальше', disabled=True))
    sel, name, note, big = (5, 'Сова', 'Выпал случайный из свободных', avatars.avatar_svg('owl', 128)) if v == 'rand' else (10, 'Персонаж 11', 'Выбран вручную', placeholder_svg(2, 128))
    hero = f'<div style="display: flex; flex-direction: column; align-items: center; gap: 8px; padding: 8px 0; flex-shrink: 0;">{big}<span style="{HEAD}">{name}</span><span style="{FN}">{note}</span>{btn("secondary", "Другой", "refresh", go="rand", label="Другой случайный персонаж")}</div>'
    return h + scroll(hero + section('Или выбрать самому · 15') + tiles(sel) + f'<span style="{FN}">Персонаж можно сменить позже. Цветная точка провайдера появится после выбора модели.</span>') + bottom(btn('primary', 'Дальше', href='NewBotConfirm.dc.html'))


page('AvatarPick.dc.html', 'Новый бот, шаг 2: персонаж', [(k, t, avatar_pick(k)) for k, t in [('rand', 'выпал случайный'), ('picked', 'выбран вручную'), ('load', 'загрузка'), ('err', 'ошибка')]])

# ---------- 7. Память бота ----------
MEM = [('gemini', 'С', 'Резюме EN для откликов лежит в ~/Documents/CV/Resume_EN.pdf', 'Скаут', 'тред «Вакансии SRE»', 'сегодня 09:50'), ('claude', 'S', 'server: диск 280 ГБ, Ubuntu, Docker', 'SRE', 'тред «Инцидент 502 webapp»', '13 сен'),
       ('claude', 'М', 'Договоры по квартире лежат в ~/Documents/Квартира', 'Мак', 'тред «Поиск договора аренды»', 'вчера 21:14'), ('claude', 'S', 'Сертификаты: certbot --nginx, без wildcard', 'SRE', 'добавлено вручную', '21 сен')]
MCHIPS = ['Все · 42', 'Скаут · 12', 'Мак · 9', 'SRE · 17', 'Кодер · 4']
MCHIPS0 = ['Все · 33', 'Скаут · 12', 'Мак · 0', 'SRE · 17', 'Кодер · 4']
M_SUB, M_SUB0 = '42 записи · у каждого бота своя', '33 записи · у каждого бота своя'


def msearch():
    i = uid()
    return f'<div style="display: flex; align-items: center; gap: 8px; height: 44px; flex-shrink: 0; padding: 0 14px; border-radius: 12px; background: var(--bg-surface); border: 1px solid var(--border-control); color: var(--fg-muted);">{ic("search", 18)}<label for="{i}" style="{SRONLY}">Поиск по памяти</label><input id="{i}" type="text" placeholder="Поиск по памяти" style="flex-grow: 1; min-width: 0; height: 42px; border: none; background: transparent; font: 400 15px/20px {SANS}; color: var(--fg-default);"></div>'


def entry(p, l, text, bot, src, when):
    return card(f'<span style="{CALL} color: var(--fg-default);">{text}</span><div style="display: flex; align-items: center; gap: 4px;"><a href="Thread.dc.html" aria-label="Источник: {bot}, {src}, {when}" style="flex-grow: 1; min-width: 0; min-height: 44px; display: flex; align-items: center; gap: 10px; text-decoration: none;">{avatar(p, l, 28)}<span style="min-width: 0; display: flex; flex-direction: column;"><span style="{FN} {ELL}">{bot} · {when}</span><span style="font: 400 13px/18px {SANS}; color: var(--focus); {ELL}">{src}</span></span></a>{iconbtn("edit", "Изменить запись", "muted", go="edit")}{iconbtn("trash", "Удалить запись", "muted", go="del")}</div>', '12px 8px 6px 14px', 6)


def quote(t):
    return f'<div style="padding: 12px 14px; border-radius: 12px; background: var(--bg-sunken); {CALL} color: var(--fg-default);">{t}</div>'


def mem_edit_form(go='data'):
    i = uid()
    return (f'<div style="display: flex; flex-direction: column; gap: 6px;"><label for="{i}" style="{FN}">Что помнит бот</label><textarea id="{i}" rows="3" style="box-sizing: border-box; padding: 10px 14px; border-radius: 12px; border: 1px solid var(--border-control); background: var(--bg-surface); color: var(--fg-default); font: 400 15px/20px {SANS}; resize: none;">Резюме EN для откликов лежит в ~/Documents/CV/Resume_EN.pdf</textarea></div>'
            + f'<div style="display: flex; align-items: center; gap: 10px;">{avatar("gemini", "С", 28)}<span style="{FN}">Скаут · из треда «Вакансии SRE» · сегодня 09:50</span></div><span style="{FN}">Бот увидит новую версию со следующего сообщения. Источник останется прежним, добавится пометка о правке.</span>'
            + grid(btn('secondary', 'Отмена', go=go) + btn('primary', 'Сохранить', go=go)))


def mem(v):
    h = hdr_back('Память', M_SUB0 if v == 'empty' else M_SUB, href='Settings.dc.html')
    add = bottom(btn('primary', 'Добавить запись', 'plus'))
    top = msearch() + (chips(MCHIPS0, 2) if v == 'empty' else chips(MCHIPS, 0))
    if v == 'empty':
        return h + scroll(top + empty('brain', 'У Мака пока нет записей', 'Бот запоминает факты из тредов сам. Запись можно добавить и вручную.')) + add
    if v == 'load':
        return h + scroll(top + loading('Загружаю память', 3, 96)) + add
    if v == 'err':
        return h + error('Память не загрузилась', 'Записи на сервере целы. Боты продолжают ими пользоваться.', btn('secondary', 'Повторить', 'refresh', go='data'))
    out = h + scroll(top + ''.join(entry(*m) for m in MEM[:3]), 'gap: 10px;') + add
    if v == 'edit':
        out += sheet('Правка записи', mem_edit_form(), close='data')
    if v == 'del':
        out += sheet('Удалить запись?', quote(MEM[0][2]) + f'<span style="{CALL} color: var(--fg-default);">Скаут перестанет это учитывать. Вернуть запись нельзя.</span>' + grid(btn('secondary', 'Отмена', go='data') + btn('danger', 'Удалить', 'trash', go='data')), close='data')
    return out


MV = [('data', 'данные'), ('edit', 'правка'), ('del', 'удаление'), ('empty', 'пусто'), ('load', 'загрузка'), ('err', 'ошибка')]
page('MemoryEntries.dc.html', 'Память бота', [(k, t, mem(k)) for k, t in MV])


def mem_d(v):
    right = btn('primary', 'Добавить запись', 'plus')
    top = f'<div style="display: flex; gap: 12px; align-items: center; flex-shrink: 0;"><div style="width: 260px; flex-shrink: 0;">{msearch()}</div>{chips(MCHIPS0, 2) if v == "empty" else chips(MCHIPS, 0)}</div>'
    if v == 'empty':
        return desk('memory', 'Память', M_SUB0, right, top + empty('brain', 'У Мака пока нет записей', 'Бот запоминает факты из тредов сам. Запись можно добавить и вручную.', btn('secondary', 'Добавить запись', 'plus')))
    if v == 'load':
        return desk('memory', 'Память', 'у каждого бота своя', right, top + loading('Загружаю память', 5, 56))
    if v == 'err':
        return desk('memory', 'Память', 'у каждого бота своя', right, error('Память не загрузилась', 'Записи на сервере целы. Боты продолжают ими пользоваться.', btn('secondary', 'Повторить', 'refresh', go='data')))
    trs = [[f'<span style="{CALL}">{t}</span>', avatar(p, l, 28) + b, f'<a href="Thread.dc.html" style="min-height: 44px; display: inline-flex; align-items: center; text-decoration: none; font: 400 13px/18px {SANS}; color: var(--focus);">{s}</a>', f'<span style="{FN}">{w}</span>',
            iconbtn('edit', 'Изменить запись', 'muted', go='edit') + iconbtn('trash', 'Удалить запись', 'muted', go='del')] for p, l, t, b, s, w in MEM]
    body = top + table(['Запись', 'Бот', 'Источник', 'Когда', ''], trs, '2.4fr 0.8fr 1.5fr 0.8fr 96px')
    aside = (f'<h2 style="margin: 0; {HEAD}">Правка записи</h2>' + mem_edit_form()) if v == 'edit' else None
    out = desk('memory', 'Память', M_SUB, right, body, aside)
    if v == 'del':
        out += dialog('Удалить запись?', quote(MEM[0][2]) + f'<span style="{CALL} color: var(--fg-default);">Скаут перестанет это учитывать. Вернуть запись нельзя.</span>' + grid(btn('secondary', 'Отмена', go='data') + btn('danger', 'Удалить', 'trash', go='data')), close='data')
    return out


page('MemoryDesktop.dc.html', 'Память на Mac', [(k, t, mem_d(k)) for k, t in MV], DW, DH)

# ---------- 8. Лента активности, пауза, расписания ----------
EVS = [('09:52', 'shield', 'attention-fg', 'Ждёт решения: отправить отклик', 'браузер · jobs.example.eu', 'gemini', 'С'), ('09:52', 'globe', 'fg-muted', 'Открыл сайт и заполнил форму', 'браузер · 4 шага · 18k токенов', 'gemini', 'С'),
       ('09:50', 'brain', 'fg-muted', 'Запомнил, где лежит резюме EN', 'память · тред «Вакансии SRE»', 'gemini', 'С'), ('09:41', 'clock', 'fg-muted', 'Запуск по расписанию «Вакансии»', 'расписание · будни 09:40', 'gemini', 'С'),
       ('03:00', 'pause', 'fg-muted', 'Пропуск: компьютер был недоступен', '«Ночной обзор» · квота не потрачена', 'claude', 'М')]
EV_OLD = ('18:20', 'x', 'danger-fg', 'Ошибка: сайт не ответил за 30 с', 'браузер · остановлено на шаге 2', 'gemini', 'С')
EV_PAUSE = ('10:02', 'pause', 'fg-muted', 'Поставлен на паузу владельцем', 'задачи в очереди ждут', 'gemini', 'С')


def ev(time, icon, col, text, meta, p=None, l=None, with_bot=False):
    b = avatar(p, l, 28) if with_bot else ''
    return f'<span style="width: 40px; flex-shrink: 0; font: 400 12px/16px {MONO}; color: var(--fg-muted);">{time}</span>{b}<span style="color: var(--{col}); display: flex;">{ic(icon, 18)}</span><span style="flex-grow: 1; min-width: 0; display: flex; flex-direction: column; gap: 2px;"><span style="{CALL} color: var(--fg-default);">{text}</span><span style="{FN} {ELL}">{meta}</span></span>'


def spend_card(zero=False):
    a, b = (0, 0) if zero else (64, 71)
    return card(meter('Расход', a, 'gemini-fg', '0 из 200k' if zero else '128k из 200k') + f'<span style="{FN}">Сегодня, токены. По API-ключам: {"$0" if zero else "$0,42"}. Сброс в 00:00.</span><div style="height: 1px; background: var(--border-default);"></div>'
                + meter('Контекст', b, 'attention-fg' if b >= 70 else 'fg-default', f'{b}%') + f'<div style="display: flex; align-items: center; gap: 8px;"><span style="flex-grow: 1; {FN}">Тред «Вакансии SRE». Сжатие само при 85%.</span>{btn("secondary", "Сжать", disabled=zero)}</div>', gap=8)


def act(v):
    paused = v == 'paused'
    h = hdr_back('Скаут', 'Активность', href='Thread.dc.html', av=SCOUT, right=badge('gemini', 'Gemini 3.1 Pro'))
    b = bottom(btn('primary', 'Возобновить', 'play', go='feed') if paused else btn('secondary', 'Пауза', 'pause', go='paused', label='Поставить бота на паузу'))
    if v == 'load':
        return h + scroll(loading('Загружаю ленту', 5, 60)) + b
    if v == 'err':
        return h + error('Лента не загрузилась', 'События записаны на сервере и появятся после повтора. Бот продолжает работать.', btn('secondary', 'Повторить', 'refresh', go='feed')) + b
    if v == 'empty':
        return h + scroll(spend_card(True) + empty('list', 'Событий пока нет', 'Здесь появится всё, что делает бот: шаги, запросы решений, запуски по расписанию.')) + b
    if paused:
        return h + scroll(banner('neutral', 'pause', 'Бот на паузе с 10:02', 'Расписания и триггеры не запускаются. Идущая задача остановлена на шаге 4.') + spend_card() + section('Сегодня') + rows([ev(*e) for e in [EV_PAUSE] + EVS[:2]]), 'gap: 10px;') + b
    return h + scroll(spend_card() + section('Сегодня') + rows([ev(*e) for e in EVS[:4]]) + section('Вчера') + rows([ev(*EV_OLD)]), 'gap: 10px;') + b


AV = [('feed', 'лента'), ('paused', 'бот на паузе'), ('empty', 'пусто'), ('load', 'загрузка'), ('err', 'ошибка')]
page('Activity.dc.html', 'Лента активности', [(k, t, act(k)) for k, t in AV])

OFF = status('attention-fg', 'На паузе: компьютер недоступен', 'pause')
SCH = [('folder', 'attention', 'Разбор папки inbox', 'Архив · Mac · каждый час', OFF, 'Пропущено 3 запуска'), ('screen', 'attention', 'Скриншот дашборда', 'Мак · Mac · пн 10:00', OFF, 'Пропущен 1 запуск'),
       ('clock', 'neutral', 'Вакансии', 'Скаут · сервер · будни 09:40', status('success-fg', 'Работает сейчас'), ''),
       ('clock', 'neutral', 'Разбор почты', 'Мак · будни 08:30', status('neutral-fg', 'На паузе вручную с 20 сен', 'pause'), '')]
NO_RUN = 'Запуск вручную вернётся, когда компьютер будет в сети.'


def sch_card(icon, tone, title, sub, st, miss, compact=False):
    off = 'недоступен' in st
    extra = st + (f'<span style="{FN}">{miss}{"" if compact else ". " + NO_RUN}</span>' if miss else '')
    if compact:
        ctl = ''
    elif 'вручную' in st:
        ctl = btn('ghost', 'Возобновить', label=f'Возобновить расписание: {title}', extra='padding: 0 8px;')
    elif off:
        ctl = f'<button type="button" disabled aria-label="Запустить сейчас: недоступно, компьютер не в сети" style="width: 44px; height: 44px; padding: 0; flex-shrink: 0; border-radius: 12px; border: none; background: var(--bg-sunken); color: var(--fg-default); opacity: 0.45; display: flex; align-items: center; justify-content: center;">{ic("play")}</button>'
    else:
        ctl = iconbtn('play', f'Запустить сейчас: {title}', 'secondary')
    return f'<div style="display: flex; align-items: center; gap: 12px; padding: {"8px 0" if compact else "12px 14px"}; {"" if compact else CARD} flex-shrink: 0;">{tile(icon, tone)}{two(title, sub, extra)}{ctl}</div>'


def sched(v):
    h = hdr_root('Рутины') + rtabs(0)
    add = actionbar(btn('primary', 'Новое расписание', 'plus')) + tabbar(1)
    if v == 'empty':
        return h + empty('clock', 'Расписаний нет', 'Расписание запускает бота по времени или по событию: webhook, новый файл в папке.') + add
    if v == 'load':
        return h + scroll(loading('Загружаю расписания', 4, 84), 'padding-top: 4px;') + add
    if v == 'err':
        return h + error('Расписания не загрузились', 'Сервер ответил 500. Запуски по времени продолжаются.', btn('secondary', 'Повторить', 'refresh', go='data')) + add
    if v == 'ok':
        ok = [(i, 'neutral', t, s, status('success-fg', 'следующий запуск через 12 мин') if 'Mac' in s else st, '') for i, _, t, s, st, _ in SCH]
        return h + scroll(''.join(sch_card(*s) for s in ok), 'padding-top: 4px; gap: 10px;') + add
    return h + scroll(banner('attention', 'cloudoff', 'MacBook Air недоступен с 08:14', '2 расписания на паузе, квота не тратится.') + ''.join(sch_card(*s) for s in SCH[:3]), 'padding-top: 4px; gap: 10px;') + add


page('Schedules.dc.html', 'Рутины: расписания', [(k, t, sched(k)) for k, t in [('data', 'компьютер недоступен'), ('ok', 'всё работает'), ('empty', 'пусто'), ('load', 'загрузка'), ('err', 'ошибка')]])


def act_d(v):
    paused = v == 'paused'
    right = btn('primary', 'Возобновить Скаута', 'play', go='feed') if paused else btn('secondary', 'Пауза Скаута', 'pause', go='paused')
    top = chips(['Все боты', 'Скаут', 'Мак', 'SRE', 'Кодер'], 0)
    if v == 'load':
        return desk('activity', 'Активность', 'Что делали боты', '', top + loading('Загружаю ленту', 6, 56), loading('Считаю расход', 3, 56))
    if v == 'err':
        return desk('activity', 'Активность', 'Что делали боты', '', error('Лента не загрузилась', 'События записаны на сервере и появятся после повтора. Боты продолжают работать.', btn('secondary', 'Повторить', 'refresh', go='feed')))
    if v == 'empty':
        return desk('activity', 'Активность', 'Что делали боты', '', top + empty('list', 'Событий пока нет', 'Здесь появится всё, что делают боты: шаги, запросы решений, запуски по расписанию.'))
    evs = ([EV_PAUSE] + EVS) if paused else EVS
    ban = banner('neutral', 'pause', 'Скаут на паузе с 10:02', 'Его расписания и триггеры не запускаются. Идущая задача остановлена на шаге 4.') if paused else ''
    body = top + ban + section('Сегодня') + rows([ev(*e, with_bot=True) for e in evs]) + section('Вчера') + rows([ev(*EV_OLD, with_bot=True)])
    aside = (f'<h2 style="margin: 0; {HEAD}">Расход сегодня</h2>' + card(meter('Скаут', 64, 'gemini-fg', '128k') + meter('SRE', 35, 'claude-fg', '70k') + meter('Мак', 12, 'claude-fg', '24k') + f'<span style="{FN}">Токены из дневного бюджета. По API-ключам: $0,42.</span>', gap=8)
             + f'<h2 style="margin: 0; {HEAD}">Контекст тредов</h2>' + card(meter('Вакансии', 71, 'attention-fg', '71%') + meter('Инцидент', 38, 'fg-default', '38%') + f'<div style="display: flex; align-items: center; gap: 8px;"><span style="flex-grow: 1; {FN}">Сжатие само при 85%.</span>{btn("secondary", "Сжать «Вакансии»")}</div>', gap=8)
             + f'<h2 style="margin: 0; {HEAD}">Расписания</h2>' + banner('attention', 'cloudoff', 'MacBook Air недоступен с 08:14', '2 расписания на паузе, квота не тратится.') + ''.join(sch_card(*s, compact=True) for s in SCH[:2]))
    return desk('activity', 'Активность', 'Что делали боты · сегодня 14 событий', right, body, aside, 400, scout='пауза' if paused else 'работает')


page('ActivityDesktop.dc.html', 'Активность на Mac', [(k, t, act_d(k)) for k, t in AV], DW, DH)

# ---------- 9. Режимы разрешений ----------
TOOLS = [('Файлы: чтение', 'просмотр файлов и поиск по ним', 'auto', 'Read, Glob, Grep'), ('Браузер', 'переходы, клики, ввод', 'ask', 'browser.*'), ('Терминал', 'команды на компьютере бота', 'ask', 'Bash'),
         ('Загрузка страниц', 'чтение сайтов без браузера', 'ask', 'WebFetch'), ('Процедуры', 'запуск записанных действий', 'cmd', 'procedure.run'), ('Управление Mac', 'клики и ввод на Mac', 'deny', 'mac.*')]
LOCKED = ['Ввод паролей', 'Оплата', 'Удаление данных', 'Установка программ']
WHY = 'Эти действия необратимы или открывают доступ к деньгам и аккаунтам. Ошибку бота здесь не исправить, поэтому решение всегда за человеком.'
WHY3 = ['Режим «Без вопроса» на них не действует.', 'Правило «Разрешать без вопроса» из карточки решения их не покрывает.', 'Пароль вводится мимо модели: бот его не видит.']


def locked_card(go=None, full=False):
    tags = ''.join(tag(t, 'attention', 'lock') for t in LOCKED)
    more = ''.join(f'<li style="{FN} color: var(--fg-default);">{t}</li>' for t in WHY3)
    tail = f'<ul style="margin: 0; padding-left: 18px; display: flex; flex-direction: column; gap: 4px;">{more}</ul>' if full else btn('ghost', 'Почему нельзя снять', 'info', go=go, extra='align-self: flex-start; padding: 0 8px;')
    return card(f'<div style="display: flex; align-items: center; gap: 8px; {CALLB}"><span style="color: var(--attention-text); display: flex;">{ic("lock", 18)}</span>Всегда с подтверждением</div><div style="display: flex; flex-wrap: wrap; gap: 6px;">{tags}</div><span style="{FN}">{WHY if full else "Подтверждение для этих действий не снимается ни режимом, ни правилом."}</span>{tail}', gap=8)


def perm(v):
    h = hdr_back('Разрешения', 'Скаут · что бот делает сам', href='Settings.dc.html', av=SCOUT)
    if v == 'empty':
        return h + scroll(empty('plug', 'У бота нет инструментов', 'Скаут может только отвечать текстом. Инструменты подключаются в настройках бота.', btn('secondary', 'Открыть настройки', href='BotSettings.dc.html')) + locked_card('why'))
    if v == 'load':
        return h + scroll(loading('Загружаю разрешения', 5, 64))
    if v == 'err':
        return h + error('Разрешения не загрузились', 'Пока список недоступен, бот спрашивает перед каждым действием.', btn('secondary', 'Повторить', 'refresh', go='data'))
    out = h + scroll(section('Инструменты · 6') + rows([two(n, d) + mode_pill(m, 'pick') for n, d, m, _ in TOOLS]) + section('Нельзя отключить') + locked_card('why'), 'gap: 10px;')
    if v == 'pick':
        opts = ''.join(radio(m == 'ask', w, d, go='data', icon=f'<span style="color: var(--{c}); display: flex;">{ic(i, 20)}</span>') for m, w, i, c, d in MODES)
        out += sheet('Терминал', f'<div role="radiogroup" aria-label="Режим разрешения" style="display: flex; flex-direction: column; gap: 6px;">{opts}</div><span style="{FN}">Действие, которое не удалось распознать, всегда требует подтверждения.</span>' + tech(['Инструмент: Bash'], 'Подробнее') + btn('primary', 'Готово', go='data'), sub='Команды на компьютере бота', close='data')
    if v == 'why':
        out += sheet('Почему нельзя снять', f'<div style="display: flex; flex-wrap: wrap; gap: 6px;">{"".join(tag(t, "attention", "lock") for t in LOCKED)}</div><span style="{CALL} color: var(--fg-default);">{WHY}</span>'
                     + '<ul style="margin: 0; padding-left: 18px; display: flex; flex-direction: column; gap: 6px;">' + ''.join(f'<li style="{CALL} color: var(--fg-default);">{t}</li>' for t in WHY3) + '</ul>' + btn('primary', 'Понятно', go='data'), close='data')
    return out


PV = [('data', 'данные'), ('pick', 'выбор режима'), ('why', 'почему нельзя снять'), ('empty', 'пусто'), ('load', 'загрузка'), ('err', 'ошибка')]
page('Permissions.dc.html', 'Режимы разрешений', [(k, t, perm(k)) for k, t in PV])


def perm_d(v):
    right = f'<button type="button" style="min-height: 44px; display: flex; align-items: center; gap: 10px; padding: 0 14px; border-radius: 12px; border: 1px solid var(--border-control); background: var(--bg-surface); color: var(--fg-default); font: 500 15px/20px {SANS};">{avatar("gemini", "С", 28)}Скаут{ic("down", 16, 2.4)}</button>'
    aside = f'<h2 style="margin: 0; {HEAD}">Нельзя отключить</h2>' + locked_card(full=True)
    if v == 'load':
        return desk('perms', 'Разрешения', 'Что бот делает сам', right, loading('Загружаю разрешения', 6, 60), aside)
    if v == 'err':
        return desk('perms', 'Разрешения', 'Что бот делает сам', right, error('Разрешения не загрузились', 'Пока список недоступен, бот спрашивает перед каждым действием.', btn('secondary', 'Повторить', 'refresh', go='data')), aside)
    if v == 'empty':
        return desk('perms', 'Разрешения', 'Что бот делает сам', right, empty('plug', 'У бота нет инструментов', 'Скаут может только отвечать текстом. Инструменты подключаются в настройках бота.', btn('secondary', 'Открыть настройки')), aside)
    legend = '<div style="display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; flex-shrink: 0;">' + ''.join(card(f'<div style="display: flex; align-items: center; gap: 8px; {CALLB}"><span style="color: var(--{c}); display: flex;">{ic(i, 18)}</span>{w}</div><span style="{FN}">{d}</span>', '12px', 6) for _, w, i, c, d in MODES) + '</div>'
    idx = {'auto': 0, 'ask': 1, 'cmd': 2, 'deny': 3}
    trs = rows([two(n, d) + f'<div style="width: 480px; flex-shrink: 0;">{seg([m[1] for m in MODES], idx[m_], f"Режим: {n}")}</div>' for n, d, m_, _ in TOOLS])
    return desk('perms', 'Разрешения', 'Скаут · что бот делает сам', right, legend + section('Инструменты · 6') + trs + f'<span style="{FN}">Действие, которое не удалось распознать, всегда требует подтверждения.</span>' + tech([f'{n}: {t}' for n, _, _, t in TOOLS], 'Подробнее: имена инструментов'), aside)


page('PermissionsDesktop.dc.html', 'Разрешения на Mac', [(k, t, perm_d(k)) for k, t in [('data', 'данные'), ('empty', 'пусто'), ('load', 'загрузка'), ('err', 'ошибка')]], DW, DH)
