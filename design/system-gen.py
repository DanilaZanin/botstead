import os, json, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import avatars
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'system')
FONTS = '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Unbounded:wght@500;600&family=Golos+Text:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap">'
BASE = """
body{margin:0;background:var(--bg-canvas);color:var(--fg-default);font-family:var(--font-sans)}
*{box-sizing:border-box}
button{font:inherit;cursor:pointer}
:focus-visible{outline:2px solid var(--focus);outline-offset:2px}
.row{display:flex;gap:var(--space-2);align-items:center;flex-wrap:wrap}
.col{display:flex;flex-direction:column;gap:var(--space-3)}
.pad{padding:var(--space-4)}
.btn{min-height:var(--hit-min);padding:0 var(--space-4);border-radius:var(--radius-md);border:1px solid transparent;font-size:.9375rem;font-weight:500;display:inline-flex;align-items:center;justify-content:center;gap:var(--space-2)}
.btn-primary{background:var(--bg-emphasis);color:var(--fg-on-emphasis)}
.btn-secondary{background:var(--bg-surface);color:var(--fg-default);border-color:var(--border-control)}
.btn-ghost{background:transparent;color:var(--fg-default)}
.btn-danger{background:var(--danger-bg);color:var(--danger-fg)}
.btn-approve{background:var(--attention-emphasis);color:var(--fg-on-attention)}
.btn[disabled]{opacity:.45;cursor:not-allowed}
.icon-btn{width:var(--hit-min);padding:0;border-radius:var(--radius-md)}
.badge{display:inline-flex;align-items:center;gap:var(--space-1);padding:2px 7px;border-radius:var(--radius-sm);font-size:.75rem;font-weight:600;letter-spacing:.02em;line-height:1rem}
.p-claude{background:var(--claude-bg);color:var(--claude-fg)}
.p-codex{background:var(--codex-bg);color:var(--codex-fg)}
.p-gemini{background:var(--gemini-bg);color:var(--gemini-fg)}
.dot{width:8px;height:8px;border-radius:4px;display:inline-block}
.muted{color:var(--fg-muted)}
.mono{font-family:var(--font-mono);font-size:.8125rem;line-height:1.125rem}
.card{background:var(--bg-surface);border:1px solid var(--border-default);border-radius:var(--radius-lg)}
@media (prefers-reduced-motion: reduce){*{animation:none!important;transition:none!important}}
"""
IC = {
 'plus':'<path d="M12 5v14M5 12h14"/>',
 'send':'<path d="M12 19V5M5 12l7-7 7 7"/>',
 'stop':'<rect x="7" y="7" width="10" height="10" rx="2"/>',
 'mic':'<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0014 0M12 18v3"/>',
 'screen':'<rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/>',
 'risk':'<path d="M12 3l9 16H3z"/><path d="M12 10v4M12 17h.01"/>',
 'check':'<path d="M5 12l5 5 9-10"/>',
 'x':'<path d="M6 6l12 12M18 6L6 18"/>',
 'chev':'<path d="M9 6l6 6-6 6"/>',
 'down':'<path d="M6 9l6 6 6-6"/>',
 'bot':'<rect x="4" y="7" width="16" height="12" rx="3"/><path d="M12 3v4M9 13h.01M15 13h.01"/>',
 'chat':'<path d="M4 5h16v11H9l-5 4z"/>',
 'shield':'<path d="M12 3l8 4v5c0 5-3.5 8-8 9-4.5-1-8-4-8-9V7z"/><path d="M9 12l2 2 4-4"/>',
 'bars':'<path d="M5 20V10M12 20V4M19 20v-7"/>',
 'clock':'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
 'spin':'<path d="M12 3a9 9 0 109 9"/>',
}
def ic(n, s=20, w=2):
    return f'<svg width="{s}" height="{s}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="{w}" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{IC[n]}</svg>'

def page(marker, body, css=''):
    return f'<!-- {marker} -->\n<!doctype html>\n<html lang="ru">\n<head>\n<meta charset="utf-8">\n{FONTS}\n<style>{BASE}{css}</style>\n</head>\n<body>\n{body}\n<script>document.documentElement.setAttribute("lang","ru");</script>\n</body>\n</html>\n'

C = {}

C['Button'] = ('Actions', 150, f"""<div class="col pad">
<div class="row">
<button class="btn btn-primary" type="button">{ic('plus',16,2.4)}Новый бот</button>
<button class="btn btn-secondary" type="button">Отклонить</button>
<button class="btn btn-ghost" type="button">Архивировать</button>
<button class="btn btn-approve" type="button">Разрешить</button>
<button class="btn btn-danger" type="button">{ic('stop',16,2.4)}Стоп</button>
</div>
<div class="row">
<button class="btn btn-primary" type="button" disabled>Отправить</button>
<button class="btn btn-secondary" type="button" aria-busy="true"><span class="spin">{ic('spin',16,2.4)}</span>Сохраняю</button>
<button class="btn btn-secondary icon-btn" type="button" aria-label="Экран бота">{ic('screen')}</button>
<button class="btn btn-primary icon-btn" type="button" aria-label="Отправить" style="border-radius:var(--radius-full)">{ic('send',18,2.4)}</button>
</div></div>""", '.spin{display:inline-flex;animation:r 1s linear infinite}@keyframes r{to{transform:rotate(360deg)}}',
"""# Button

Кнопка действия; высота всегда 44px (`hit-min`), текст в стиле `callout`.

## Варианты
| Вариант | Когда | Токены |
| --- | --- | --- |
| primary | Одно главное действие на экране: «Новый бот», «Отправить» | `bg-emphasis`, `fg-on-emphasis` |
| secondary | Второстепенное рядом с главным: «Отклонить», «Отмена» | `bg-surface`, `border-control`, `fg-default` |
| ghost | Третьестепенное в тулбарах и меню: «Архивировать» | `fg-default` без фона |
| approve | Только в карточке риска: «Разрешить» | `attention-emphasis`, `fg-on-attention` |
| danger | «Стоп» бота во время работы, удаление треда по явному id | `danger-bg`, `danger-fg` |

Кнопка-иконка: квадрат 44×44 (`radius-md`) или круг (`radius-full`) для «Отправить» и «Голос»; всегда с `aria-label`.

## Состояния
- default, hover (фон темнее на 6%), pressed (темнее на 12%), focus (кольцо `focus` 2px со смещением 2px).
- disabled: непрозрачность 45%, `cursor: not-allowed`, не получает фокус через Tab только если действие невозможно в принципе.
- loading: иконка-спиннер слева, текст в форме «Сохраняю», `aria-busy="true"`, повторное нажатие игнорируется.

## Доступность
- Настоящий `<button type="button">`; ссылка-переход оформляется как `<a>`, не как кнопка.
- Enter и Space нажимают; скринридер читает текст кнопки или `aria-label`.

## Делай / не делай
| Делай | Не делай |
| --- | --- |
| Глагол, что произойдёт: «Разрешить», «Отправить отклик» | «ОК», «Да» без контекста |
| Одна primary на экран | Две primary рядом |
| «Стоп» видна весь ход работы бота | Прятать «Стоп» в меню |
""")

C['ProviderBadge'] = ('Identity', 90, f"""<div class="row pad">
<span class="badge p-claude">Claude · Opus 5.5</span>
<span class="badge p-codex">Codex · GPT-6 Sol</span>
<span class="badge p-gemini">Gemini 3.1 Pro</span>
</div>""", '', """# ProviderBadge

Метка провайдера и модели бота; тот же цвет красит аватар бота (первая буква имени, шрифт `display`).

## Варианты
| Провайдер | Текст | Фон |
| --- | --- | --- |
| Claude | `claude-fg` | `claude-bg` |
| Codex (OpenAI) | `codex-fg` | `codex-bg` |
| Gemini | `gemini-fg` | `gemini-bg` |

Размеры: бейдж (стиль `caption`, `radius-sm`), аватар 44px в списке и 36px в шапке треда (`radius-md`).

## Правила
- Бейдж всегда содержит слово: имя модели. Цвет помогает узнать провайдера, но не заменяет подпись.
- Цвета провайдеров не используются ни для чего, кроме провайдеров: не для статусов и не для кнопок.
- Модель меняется только через выбор модели в панели ввода или в карточке бота; бейдж не кликабелен.

## Доступность
- Аватар декоративный рядом с именем бота: `aria-hidden="true"`.
""")

C['StatusBadge'] = ('Identity', 90, f"""<div class="row pad" style="gap:var(--space-4)">
<span class="row footnote" style="gap:6px"><span class="dot" style="background:var(--success-fg)"></span>Работает</span>
<span class="row footnote" style="gap:6px"><span class="dot" style="background:var(--attention-fg)"></span>Ждёт подтверждения</span>
<span class="row footnote" style="gap:6px"><span class="dot" style="background:var(--neutral-fg)"></span>Спит до 09:00</span>
<span class="row footnote" style="gap:6px;color:var(--danger-fg)">{ic('x',14,2.6)}Ошибка: таймаут</span>
</div>""", '', """# StatusBadge

Состояние бота или треда: цветная точка 8px плюс слово в стиле `footnote`.

## Состояния
| Состояние | Цвет | Текст |
| --- | --- | --- |
| Работает | `success-fg` | «Работает» |
| Ждёт подтверждения | `attention-fg` | «Ждёт подтверждения» |
| Спит / по расписанию | `neutral-fg` | «Спит до 09:00» |
| Ошибка | `danger-fg`, иконка ✕ вместо точки | «Ошибка: <причина>» |

## Правила
- Никогда только цвет: слово обязательно, у ошибки ещё и иконка (зелёный и красный не различаются по оттенку).
- «Ждёт подтверждения» дублируется точкой на вкладке «Подтверждения» в таб-баре.
""")

steps = [('done','Открыл linkedin.com/jobs','browser.open'),('done','Фильтр: SRE, remote, EU → 23','browser.fill'),('running','Проверяю релокацию в Сербию','browser.read'),('queued','Сохранить таблицу','files.write'),('error','Отклик на vacancy/812: форма не найдена','browser.submit_form')]
def step(s,t,tool):
    mark = {'done':f'<span style="color:var(--success-fg)">{ic("check",16,2.6)}</span>','running':f'<span class="spin" style="color:var(--fg-default)">{ic("spin",16,2.6)}</span>','queued':'<span class="dot" style="background:var(--neutral-fg);margin:4px"></span>','error':f'<span style="color:var(--danger-fg)">{ic("x",16,2.6)}</span>'}[s]
    label={'done':'готово','running':'работает','queued':'в очереди','error':'ошибка'}[s]
    return f'<li style="display:flex;gap:10px;align-items:flex-start"><span style="width:16px;display:flex;justify-content:center;padding-top:1px" aria-label="{label}">{mark}</span><span style="display:flex;flex-direction:column"><span class="callout" style="font-weight:400">{t}</span><span class="mono muted">{tool}</span></span></li>'
C['AgentPlan'] = ('Agent', 330, f"""<div class="pad"><div class="card" style="padding:var(--space-3) var(--space-4);max-width:360px">
<div class="row" style="justify-content:space-between"><span class="footnote muted" style="font-weight:600">План · 5 шагов · 3 мин</span><button class="btn btn-ghost" type="button" style="min-height:32px;padding:0 8px" aria-expanded="true">Свернуть</button></div>
<ol style="list-style:none;margin:var(--space-2) 0 0;padding:0;display:flex;flex-direction:column;gap:var(--space-2)">{''.join(step(*x) for x in steps)}</ol>
<details style="margin-top:var(--space-3)"><summary class="footnote muted" style="cursor:pointer;min-height:32px;display:flex;align-items:center">Аргументы шага 5</summary><pre class="mono" style="margin:0;padding:var(--space-2) var(--space-3);background:var(--bg-sunken);border-radius:var(--radius-md);white-space:pre-wrap">{{"url": "jobs.example.eu/812",
 "form": "#apply"}}</pre></details>
</div></div>""", '.spin{display:inline-flex;animation:r 1s linear infinite}@keyframes r{to{transform:rotate(360deg)}}', """# AgentPlan

Лента шагов агента: что бот делает сейчас, что сделал и что впереди. Показывается над текстом ответа.

## Что передаёт потребитель
- Список шагов: `status` (queued | running | done | error), человеческий текст, имя инструмента, аргументы (JSON, необязательно).
- Итог после завершения: число шагов и время.

## Состояния шага
| Статус | Отметка | Токен |
| --- | --- | --- |
| в очереди | точка | `neutral-fg` |
| работает | вращающаяся дуга | `fg-default` |
| готово | галочка | `success-fg` |
| ошибка | крестик + причина словами | `danger-fg` |
| ждёт решения | щит | `attention-fg` |

## Правила
- Текст шага человеческий («Фильтр: SRE, remote, EU → 23»), имя инструмента ниже моношрифтом `log`.
- Аргументы JSON только в свёрнутом `<details>`, по умолчанию закрыт.
- План появляется сразу, до потока текста; при стриминге дописываются новые строки, старые не перерисовываются.
- После завершения план сворачивается в строку «5 шагов · 3 мин», раскрывается по нажатию.
- В ходе процедуры и в ленте шагов браузера отметка стоит в круге 24px на `bg-sunken`, у шага в очереди в круге его номер. Сбойный шаг получает подложку `danger-bg` и кнопки «Передать боту», «Повторить»; шаг «ждёт решения» получает подложку `attention-bg` и кнопки `ApprovalCard`.
- Токены и стоимость здесь не показываются: они в подвале сообщения после завершения (Message).

## Доступность
- `<ol>`; у каждой отметки `aria-label` со статусом словами; живой регион `aria-live="polite"` только для строки «работает».
""")

C['Message'] = ('Agent', 330, f"""<div class="col pad" style="max-width:390px">
<div class="footnote muted" style="align-self:center;background:var(--bg-sunken);padding:4px 10px;border-radius:10px">Начато на Mac · 21:14</div>
<div class="body" style="align-self:flex-end;max-width:290px;padding:12px 14px;border-radius:16px 16px 4px 16px;background:var(--bg-emphasis);color:var(--fg-on-emphasis)">Найди 5 удалённых вакансий SRE в Европе с релокацией в Сербию.</div>
<div class="card" style="max-width:320px;padding:12px 14px;border-radius:16px 16px 16px 4px">
<div class="body">Нашёл 7, сохранил 5 лучших в <a href="#" style="color:var(--focus)">sre-jobs.csv</a>.</div>
<div class="row mono muted" style="margin-top:8px;gap:12px"><span>41k токенов</span><span>3 мин 12 с</span><span class="badge p-gemini">Gemini 3.1 Pro</span></div>
</div>
<div class="footnote" style="padding:10px 12px;border-radius:12px;background:var(--danger-bg);color:var(--danger-fg);display:flex;gap:8px;align-items:center">{ic('stop',14,2.6)}Остановлено на шаге 3 из 5. Таблица сохранена, отклик не отправлен.</div>
</div>""", '', """# Message

Реплика в треде: владелец справа (`bg-emphasis`), бот слева (`bg-surface` с границей), служебная плашка по центру.

## Варианты
| Вариант | Где | Токены |
| --- | --- | --- |
| owner | Сообщение владельца | `bg-emphasis`, `fg-on-emphasis`, хвост справа снизу |
| bot | Ответ бота | `bg-surface`, `border-default`, хвост слева снизу |
| system | Устройство и время начала («Начато на Mac · 21:14»), смена модели | `bg-sunken`, `fg-muted`, `footnote` |
| stopped | Бот остановлен: где, что сохранено, что отменено | `danger-bg`, `danger-fg` |

## Правила
- Текст сообщений в стиле `body` (17px), ширина пузыря до 80% экрана.
- Подвал ответа бота: токены, время, модель. Показывается только после завершения, без бегущих счётчиков.
- Системная плашка устройства появляется, когда тред продолжается с другого устройства: это и есть «продолжить на Mac».
- При остановке всегда три факта: на каком шаге, что сохранено, что отменено.

## Доступность
- Тред `role="log"`, новые ответы объявляются `aria-live="polite"` после завершения, не по чанкам.
""")

C['ApprovalCard'] = ('Agent', 330, f"""<div class="pad"><div style="max-width:360px;padding:var(--space-4);border-radius:var(--radius-lg);background:var(--attention-bg);border:1px solid var(--attention-border);display:flex;flex-direction:column;gap:var(--space-3)">
<div class="row caption" style="color:var(--attention-text);gap:6px">{ic('risk',16,2.2)}Отправка · нужно подтверждение</div>
<div class="callout" style="color:var(--fg-default);font-weight:500">Скаут хочет отправить отклик на «Senior SRE, Belgrade/remote»</div>
<dl class="footnote" style="margin:0;display:grid;grid-template-columns:auto 1fr;gap:4px 12px;color:var(--attention-text)"><dt>Куда</dt><dd style="margin:0">jobs.example.eu/812</dd><dt>Данные</dt><dd style="margin:0">Резюме EN, email</dd><dt>Отменить</dt><dd style="margin:0">Нельзя</dd></dl>
<div class="mono" style="color:var(--attention-text)">sha 4f2a…9c1 · истекает через 27 мин</div>
<div style="display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:var(--space-2)">
<button class="btn btn-secondary" type="button">Отклонить</button>
<button class="btn btn-secondary" type="button">Изменить</button>
<button class="btn btn-approve" type="button">Разрешить</button>
</div></div></div>""", '', """# ApprovalCard

Карточка риска: бот на паузе и ждёт решения перед действием, которое нельзя отменить или которое стоит денег.

## Что передаёт потребитель
- Категория риска: Оплата, Отправка, Удаление, Вход в аккаунт, Другое.
- Одна фраза «кто и что хочет сделать», затем три факта: куда, какие данные, можно ли отменить.
- Хеш аргументов и срок действия (по умолчанию 30 минут, потом отказ).

## Действия
| Кнопка | Что делает |
| --- | --- |
| Отклонить (secondary) | Бот получает отказ и продолжает без действия |
| Изменить (secondary) | Открывает аргументы для правки; изменённые аргументы получают новый хеш |
| Разрешить (approve) | Выполняет ровно эти аргументы; другие аргументы требуют новой карточки |

## Правила
- Фон `attention-bg`, граница `attention-border`, текст `attention-text`; кнопка «Разрешить» справа.
- Карточка встраивается в тред и дублируется на вкладке «Подтверждения», push ведёт прямо на неё.
- Решение пишется в историю: кто, с какого устройства, когда.
- Никогда не показывать «Разрешить все» в MVP.

## Доступность
- `role="alertdialog"` только в листе на весь экран; в треде это обычная секция с заголовком.
- Фокус по умолчанию на «Отклонить», не на «Разрешить».
""")

C['QuotaMeter'] = ('Data', 170, """<div class="pad"><div class="card" style="padding:14px 16px;max-width:360px;display:flex;flex-direction:column;gap:10px">
<div class="row footnote muted" style="justify-content:space-between"><span>Квоты подписок на неделю</span><span>сброс пн 03:00</span></div>
""" + ''.join(f'<div class="row footnote" style="flex-wrap:nowrap;gap:10px"><span style="width:64px;font-weight:600">{n}</span><div role="meter" aria-valuenow="{p}" aria-valuemin="0" aria-valuemax="100" aria-label="{n}: {p}%" style="flex-grow:1;height:8px;background:var(--bg-sunken);border-radius:4px;overflow:hidden"><div style="height:8px;width:{p}%;background:var(--{c});border-radius:4px"></div></div><span class="mono muted" style="width:40px;text-align:right">{p}%</span></div>' for n,p,c in [('Claude',42,'claude-fg'),('Codex',18,'codex-fg'),('Gemini',7,'gemini-fg')]) + """
<div class="row footnote" style="color:var(--attention-text);background:var(--attention-bg);border-radius:8px;padding:6px 10px">Кодер потратил 80% дневного бюджета</div>
</div></div>""", '', """# QuotaMeter

Полоса расхода квоты подписки или дневного бюджета бота.

## Что передаёт потребитель
- Имя провайдера или бота, процент, время сброса.

## Правила
- Заливка в цвете провайдера (`claude-fg`, `codex-fg`, `gemini-fg`), трек `bg-sunken`, число справа моношрифтом.
- От 80%: строка-предупреждение `attention-bg` / `attention-text`. 100%: `danger-bg` / `danger-fg`, бот не запускает новые шаги.
- Проценты обновляются раз в час и после каждой задачи, не в реальном времени.

## Доступность
- `role="meter"` с `aria-valuenow`, `aria-label` включает имя и процент.
""")

C['BotCard'] = ('Data', 130, """<div class="pad"><a href="#" class="card" style="display:flex;gap:12px;padding:14px;max-width:360px;text-decoration:none;color:var(--fg-default)">
""" + avatars.avatar_html("scout","gemini") + """
<span style="display:flex;flex-direction:column;gap:4px;min-width:0">
<span class="row" style="gap:8px"><span class="headline">Скаут</span><span class="badge p-gemini">Gemini 3.1 Pro</span></span>
<span class="footnote muted" style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis">Нашёл 7 вакансий SRE, жду решения по отклику</span>
<span class="row footnote muted" style="gap:10px"><span class="row" style="gap:5px"><span class="dot" style="background:var(--attention-fg)"></span>Ждёт подтверждения</span><span>Сервер</span></span>
</span></a></div>""", '', """# BotCard

Строка списка ботов: персонаж `BotAvatar` с точкой провайдера, имя, модель, последнее действие, статус и где работает.

## Что передаёт потребитель
- Имя, провайдер и модель, последняя строка активности, статус (StatusBadge), исполнитель: «Сервер» или «Mac».

## Правила
- Вся карточка одна ссылка на тред бота; внутри нет других интерактивных элементов.
- Последняя строка обрезается многоточием в одну строку.
- Порядок в списке: сначала ждущие подтверждения, потом работающие, потом спящие.
- Долгое нажатие открывает меню: Новая задача (сброс контекста), Экран, Настройки, Архив.
""")

C['Composer'] = ('Actions', 190, f"""<div class="col pad" style="max-width:390px">
<div class="card" style="padding:10px 12px;display:flex;flex-direction:column;gap:8px">
<div class="row" style="gap:8px"><button type="button" class="badge p-gemini" style="border:none;min-height:32px;padding:0 10px">Gemini 3.1 Pro {ic('down',12,2.6)}</button>
<label class="row footnote muted" style="gap:6px"><input id="dry" type="checkbox" style="width:18px;height:18px;margin:0">Пробный прогон</label></div>
<div class="row" style="flex-wrap:nowrap"><label for="m1" style="position:absolute;width:1px;height:1px;overflow:hidden">Сообщение</label>
<input id="m1" class="callout" placeholder="Сообщение Скауту" style="flex-grow:1;height:44px;padding:0 16px;border-radius:var(--radius-full);border:1px solid var(--border-control);background:var(--bg-sunken);color:var(--fg-default)">
<button class="btn btn-ghost icon-btn" type="button" aria-label="Голосовой ввод" style="border-radius:var(--radius-full)">{ic('mic')}</button>
<button class="btn btn-primary icon-btn" type="button" aria-label="Отправить" style="border-radius:var(--radius-full)">{ic('send',18,2.4)}</button></div></div>
<div class="card" style="padding:10px 12px"><div class="row" style="flex-wrap:nowrap"><span class="callout muted" style="flex-grow:1">Скаут работает · шаг 3 из 5</span>
<button class="btn btn-danger" type="button">{ic('stop',16,2.4)}Стоп</button></div></div>
</div>""", '', """# Composer

Панель ввода внизу треда: выбор модели, пробный прогон, поле, голос, отправка. Пока бот работает, вместо поля видна строка прогресса и кнопка «Стоп».

## Состояния
| Состояние | Что видно |
| --- | --- |
| Готов | Модель, «Пробный прогон», поле, голос, «Отправить» |
| Бот работает | «<Бот> работает · шаг N из M» и «Стоп» (danger) |
| Пробный прогон включён | Бейдж «Пробный прогон» над полем; бот только читает, ничего не отправляет и не меняет |

## Правила
- Модель меняется здесь и действует со следующего сообщения; в треде появляется системная плашка «Модель: …».
- Панель на `bg-surface`, прибита к низу с отступом `env(safe-area-inset-bottom)`.
- Поле `radius-full`, высота 44px, растёт до 5 строк.
- Отправка: кнопка или Cmd+Enter на Mac; Enter на телефоне переносит строку.
""")

C['TabBar'] = ('Navigation', 110, "<div style=\"padding:var(--space-4) 0;background:linear-gradient(var(--claude-bg),var(--gemini-bg))\"><nav aria-label=\"Разделы\" style=\"display:grid;grid-template-columns:repeat(4,minmax(0,1fr));padding:6px 8px 10px;background:var(--bg-glass);-webkit-backdrop-filter:blur(20px);backdrop-filter:blur(20px);border-top:1px solid var(--border-default);max-width:390px\">" + ''.join(f'<a href="#" {"aria-current=\"page\"" if a else ""} class="caption" style="display:flex;flex-direction:column;align-items:center;gap:3px;min-height:44px;justify-content:center;text-decoration:none;position:relative;color:var(--{"fg-default" if a else "fg-muted"})">{ic(i,22)}{t}{"<span class=\"dot\" style=\"position:absolute;top:2px;right:28%;background:var(--attention-fg)\" aria-label=\"есть ожидающие\"></span>" if d else ""}</a>' for i,t,a,d in [('bot','Боты',True,False),('clock','Рутины',False,False),('shield','Решения',False,True),('bars','Расход',False,False)]) + "</nav></div>", '', """# TabBar

Нижняя навигация PWA на телефоне: Боты, Рутины, Решения, Расход. Треды открываются из карточки бота. На Mac её заменяет боковая панель с теми же разделами.

## Правила
- Фон `bg-glass` с `backdrop-filter: blur(20px)` поверх контента (материал в духе Liquid Glass), граница сверху `border-default`.
- Отступ снизу `env(safe-area-inset-bottom)`; зона каждой вкладки не меньше 44px.
- Активная вкладка `fg-default` и `aria-current="page"`, остальные `fg-muted`.
- Точка `attention-fg` на «Решения», пока есть ожидающие подтверждения.
- Не больше 4 вкладок; Память и Настройки открываются из шапки экрана «Боты».
""")


C['BotAvatar'] = ('Identity', 240, '<div class="col pad"><div class="row" style="gap:14px">' + ''.join(f'<span style="display:flex;flex-direction:column;align-items:center;gap:6px;width:64px">{avatars.avatar_html(k,p,56)}<span class="footnote muted">{n}</span></span>' for k,p,n in [('scout','gemini','Скаут'),('mac','claude','Мак'),('sre','claude','SRE'),('coder','codex','Кодер'),('archive','gemini','Архив'),('owl',None,'Сова'),('spark',None,'Искра'),('robot',None,'Робот')]) + '</div><div class="row" style="gap:12px;align-items:flex-end">' + avatars.avatar_html('sre','claude',96) + avatars.avatar_html('sre','claude',44) + avatars.avatar_html('sre','claude',36) + avatars.avatar_html('sre','claude',32) + '<span class="footnote muted">96 · 44 · 36 · 32 px</span></div></div>', '', """# BotAvatar

Персонаж бота: объёмная «пластилиновая» фигурка на пастельном фоне плюс точка провайдера в углу. Делает ботов узнаваемыми с первого взгляда.

## Что передаёт потребитель
- `kind`: scout, mac, sre, coder, archive, owl, spark, robot.
- `provider`: claude, codex, gemini (точка в углу) или ничего для превью в выборе.
- `size`: 96 (настройки бота), 44 (список), 36 (шапка треда), 32 (боковая панель Mac).

## Персонажи
| kind | Кто | Фон |
| --- | --- | --- |
| scout | Скаут: оранжевый, подзорная труба и флажок | `avatar-sky` |
| mac | Мак: бирюзовый экран с пиксельными глазами | `avatar-peach` |
| sre | SRE: терракотовый, каска с мигалкой | `avatar-mint` |
| coder | Кодер: фиолетовый, глаза < > и курсор | `avatar-lilac` |
| archive | Архив: кофейная коробка в очках | `avatar-sand` |
| owl | Сова: исследователь | `avatar-lime` |
| spark | Искра: идеи | `avatar-peach` |
| robot | Робот: универсальный | `avatar-sky` |

## Правила
- Объём: градиент тела со светом сверху слева, блик, внутренняя тень по силуэту, мягкая тень-эллипс под фигурой. Контур `avatar-ink`. Фон плитки из палитры `avatar-*`. Одинаково в обеих темах.
- Только SVG-градиенты и полупрозрачные формы, без фильтров и растровых картинок: так аватар чёткий на 32px и лёгкий на странице с десятками ботов.
- Id градиентов уникальны на каждый экземпляр (генератор добавляет счётчик).
- Точка провайдера: диаметр 30% аватара, цвет `claude-fg`, `codex-fg` или `gemini-fg`, обводка 2px `bg-surface`.
- Скругление фона 12px при размере 44 (пропорционально на других размерах).
- Новый бот получает свободного персонажа; «Другой» в настройках перебирает оставшихся.
- Не рисовать поверх аватара статус: статус живёт в `StatusBadge` рядом с именем.

## Доступность
- Рядом всегда есть имя бота, поэтому аватар `aria-hidden="true"`.
- В выборе персонажа каждая плитка это `<button>` с `aria-label` «Персонаж: Сова» и `aria-pressed`.
""")


# ---- Компоненты этапа 2 (экраны design/screens2-gen.py). Превью собраны теми же кирпичами, что и экраны. ----
import screens2_lib as L  # noqa: E402

SPIN = '.spin{display:inline-flex;animation:r 1s linear infinite}@keyframes r{to{transform:rotate(360deg)}}'
box = lambda inner, w=360: f'<div class="pad"><div style="max-width:{w}px;display:flex;flex-direction:column;gap:12px">{inner}</div></div>'  # noqa: E731

C['TextField'] = ('Forms', 400, box(L.field('Логин', 'admin', hint='Латиница и цифры, от 3 символов') + L.field('Пароль', '000000000000', typ='password', err='Логин или пароль не подходят', right=L.iconbtn('eye', 'Показать пароль', 'ghost'))
                                    + L.field('Base URL', 'http://192.168.1.20:11434/v1', mono=True) + L.field('Название', 'Anthropic API', disabled=True)), '', """# TextField

Поле ввода с подписью: вход, инвайт, ключ провайдера, параметры процедуры, пароль мимо модели.

## Что передаёт потребитель
- `label` (обязательна, всегда видна над полем), `value`, `placeholder`, `type` (text, password).
- `hint`: пояснение под полем в стиле `footnote`. `error`: причина словами, заменяет `hint`.
- `mono`: моноширинный текст для ключей, адресов и кодов. `right`: кнопка 44px справа (показать пароль, вставить из буфера).

## Состояния
| Состояние | Что видно |
| --- | --- |
| Обычное | фон `bg-surface`, граница 1px `border-control` (контраст границы к фону от 3:1 в обеих темах) |
| Фокус | кольцо 2px `focus` |
| Ошибка | граница 2px `danger-fg`, под полем иконка риска и причина, `aria-invalid` |
| Выключено | прозрачность 0.6, `disabled` |

## Правила
- Высота 44px, скругление `radius-md`, текст `callout`.
- Подпись не заменяется плейсхолдером: плейсхолдер только пример значения.
- Ошибка называет причину и что делать: «Логин alice уже занят», не «Ошибка».
- Поле не кладём на `bg-sunken`: граница `border-control` на нём даёт меньше 3:1 в светлой теме.
- Секрет (пароль, API-ключ) всегда `type="password"`; значение в ленту, запись и память не попадает.

## Доступность
- `<label for>` плюс `aria-describedby` на подсказку или ошибку.
""")

C['Switch'] = ('Forms', 150, box(L.card(L.switch(True, 'Opus 5.5', 'claude-opus-5-5 · контекст 200k') + L.switch(False, 'Haiku 5', 'claude-haiku-5 · контекст 200k') + L.switch(False, 'Opus 5', 'провайдер недоступен', disabled=True), '4px 14px', 0)), '', """# Switch

Переключатель в строке списка: модель включена или нет, «Пробный прогон», «Сохранить в хранилище секретов».

## Состояния
| Состояние | Дорожка | Ручка |
| --- | --- | --- |
| Включён | `bg-emphasis` | справа, `fg-on-emphasis` |
| Выключен | `bg-surface` с границей `border-control` | слева, `fg-muted` |
| Недоступен | прозрачность 0.5 | не нажимается |

## Правила
- Действует сразу, без кнопки «Сохранить». Если нужно подтверждение, это не Switch, а кнопка.
- Дорожка 52x32, зона нажатия 52x44; строка не ниже `hit-min`.
- Состояние видно положением ручки, а не только цветом.

## Доступность
- `<button role="switch" aria-checked>` с `aria-labelledby` на подпись строки.
""")

C['SegmentedControl'] = ('Forms', 150, box(L.seg(['API-ключ', 'Endpoint', 'Подписка'], 0, 'Вид провайдера') + L.seg(['Без вопроса', 'Спросить', 'По команде', 'Запрещено'], 1, 'Режим'), 480), '', """# SegmentedControl

Выбор одного из 2–4 вариантов на месте: вид провайдера, роль в инвайте, срок ссылки, режим разрешения на Mac.

## Правила
- Подложка `bg-sunken`, скругление `radius-md`; выбранный сегмент на `bg-surface` с границей `border-default` и весом 600.
- Сегмент не ниже 44px. Подпись в одно-два слова; если не помещается в четыре сегмента на телефоне, нужен список с радиокнопками в `Sheet`.
- Выбор виден фоном и весом текста, цвет состояния не используется.

## Доступность
- `role="radiogroup"` с `aria-label`, сегменты `role="radio"` с `aria-checked`.
""")

C['Banner'] = ('Feedback', 330, box(L.banner('neutral', 'bot', 'Управляет бот', 'Шаг 4 из 6: заполняет форму отклика.') + L.banner('attention', 'hand', 'Управляете вы', 'Бот остановлен, ожидавшие действия отменены.')
                                  + L.banner('neutral', 'spin', 'Возврат боту', 'Бот заново читает страницу.') + L.banner('danger', 'cloudoff', 'Связь с терминалом потеряна', 'Вход не завершён.') + L.banner('success', 'check', 'Вход выполнен', 'Claude Code подключён.')), SPIN, """# Banner

Строка состояния экрана: кто управляет браузером, почему расписания на паузе, чем кончился вход или пробный запрос.

## Виды
| Вид | Когда | Токены |
| --- | --- | --- |
| neutral | справка, идёт процесс | `bg-sunken`, `border-default`, текст `fg-default` |
| attention | нужен человек или есть риск | `attention-bg`, `attention-border`, `attention-text` |
| danger | сбой, связь потеряна | `danger-bg`, `danger-fg` |
| success | готово | `bg-surface`, иконка `success-fg` |

## Правила
- Первая строка говорит, что происходит («Управляет бот»), вторая что это значит («Шаг 4 из 6: заполняет форму»).
- Всегда иконка плюс слово; у процесса вместо иконки вращающаяся дуга.
- Один баннер на экран. Действие лежит кнопкой под баннером или в нижней панели, не внутри.
- Ошибка называет причину и что сохранено.

## Доступность
- `role="status"`; для сбоя, который прервал действие, `role="alert"`.
""")

C['ScreenState'] = ('Feedback', 320, '<div class="pad" style="display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;width:960px">' + ''.join(f'<div class="card" style="min-height:260px;display:flex;flex-direction:column;padding:12px">{x}</div>' for x in [
    L.empty('plug', 'Провайдеров нет', 'Боты не смогут отвечать, пока не подключена хотя бы одна модель.', L.btn('primary', 'Добавить провайдера', 'plus')),
    L.loading('Проверяю провайдеров', 3, 56),
    L.error('Провайдеры не загрузились', 'Сервер не ответил за 10 с. Боты работают на прежних моделях.', L.btn('secondary', 'Повторить', 'refresh'))]) + '</div>', SPIN, """# ScreenState

Три служебных состояния любого экрана со списком: пусто, загрузка, ошибка.

## Состояния
| Состояние | Что видно |
| --- | --- |
| Пусто | иконка в круге `bg-sunken`, заголовок `headline`, одна-две строки `footnote`, главное действие |
| Загрузка | строка «Загружаю …» с дугой и скелетные карточки на `bg-sunken` по форме будущего списка |
| Ошибка | иконка риска в круге `danger-bg`, причина, что сохранено, кнопка «Повторить» |

## Правила
- Пустое состояние объясняет, зачем раздел, и даёт первое действие («Добавить провайдера»).
- Скелет без мерцания: анимация в системе одна, вращение дуги.
- Ошибка всегда говорит, что уцелело: «Записи на сервере целы», «Провайдер не сохранён».
- Шапка и навигация остаются на месте, меняется только содержимое.

## Доступность
- Загрузка: `role="status"` и `aria-busy="true"`. Ошибка: `role="alert"`.
""")

C['Sheet'] = ('Overlays', 420, '<div style="position:relative;height:400px;max-width:393px;background:var(--bg-canvas);overflow:hidden">' + L.sheet('Новый инвайт', L.field('Для кого', 'Миша') + L.seg(['1 день', '3 дня', '7 дней'], 1, 'Срок ссылки')
              + '<div style="display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px">' + L.btn('secondary', 'Отмена') + L.btn('primary', 'Создать ссылку') + '</div>', sub='Ссылка одноразовая', close='x') .replace(' onClick="{{go_x}}"', '') + '</div>', '', """# Sheet

Нижний лист телефона: короткая форма или выбор поверх экрана (инвайт, модель бота, режим разрешения, правка записи памяти, ввод пароля мимо модели, лента шагов). На Mac тот же блок показывается диалогом по центру.

## Правила
- Затемнение фона, скругление `radius-lg` сверху, тень `shadow-sheet`, ручка 36x5.
- Заголовок в стиле `title`, под ним одна строка контекста (бот, сайт, источник).
- Одно главное действие справа внизу, «Отмена» слева. Отступ снизу `env(safe-area-inset-bottom)`.
- Лист не выше 80% экрана; длинное содержимое прокручивается внутри.
- Необратимое действие в листе повторяет, что будет потеряно: «Скаут перестанет это учитывать. Вернуть запись нельзя.»

## Доступность
- `role="dialog"`, `aria-modal="true"`, `aria-labelledby` на заголовок; фокус уходит в лист и возвращается на вызвавшую кнопку; Esc и кнопка «Закрыть» закрывают.
""")

T_LINES = [('$ claude login', 'm'), ("Browser didn't open? Use the url below to sign in:", ''), ('https://claude.ai/oauth/authorize?code=true&amp;client_id=9d1c25&amp;scope=user:inference', 'link'), ('Paste code here if prompted &gt; ****', ''), ('Login successful.', 'ok'), ('OAuth error: invalid_grant', 'err')]
C['Terminal'] = ('Agent', 300, f'<div class="pad"><div style="max-width:377px;display:flex;flex-direction:column">{L.term(T_LINES, 190)}<div style="height:8px"></div>' + L.keybar([('Esc', ''), ('Tab', ''), ('Ctrl', ''), ('↑', 'Стрелка вверх'), ('↓', 'Стрелка вниз'), (L.ic('paste', 16) + 'Вставить', '')]) + '</div></div>', '', """# Terminal

Встроенный терминал (xterm.js) для входа по подписке CLI: `claude login`, `codex login`, `agy`.

## Правила
- Фон `bg-surface`, граница `border-control`, текст `mono` в `fg-default`; приглушённое `fg-muted`, успех `success-fg`, ошибка `danger-fg`, ссылка `focus` с подчёркиванием. Своих цветов у терминала нет, тема та же, что у приложения.
- Телефон: кегль 12px, межстрочный 18px, 49 колонок на ширине 393. Ядро сообщает pty реальное число колонок, CLI переносит строки сам; длинные адреса переносятся по любому символу.
- Mac: кегль 13px, 80x24.
- Над терминалом всегда `Banner` «что сейчас происходит» с номером шага. Ссылка для входа и поле кода вынесены из терминала в обычные кнопку и `TextField`: попадать пальцем в текст терминала не нужно.
- Над экранной клавиатурой ряд клавиш 44px: Esc, Tab, Ctrl, стрелки, «Вставить».
- Потеря связи: терминал приглушается, сверху `Banner` danger и кнопка «Подключиться снова».
- Число колонок, имена контейнеров и томов в интерфейсе не показываем: в шапке только «Подписка Claude · терминал на сервере».
- Поле «Код из браузера» остаётся на экране и при открытой клавиатуре, над рядом клавиш.

## Доступность
- `role="log"` с `aria-label`; фокусируется с клавиатуры. Клавиши: `role="toolbar"`, у стрелок `aria-label`.
""")

C['PermissionMode'] = ('Agent', 330, box(''.join(f'<div class="row" style="flex-wrap:nowrap;gap:12px">{L.mode_pill(m[0])}<span class="footnote muted">{m[4]}</span></div>' for m in L.MODES)
                                        + f'<div class="row" style="flex-wrap:nowrap;gap:12px">{L.mode_pill("ask", locked=True)}<span class="footnote muted">Неотключаемый список: режим сменить нельзя.</span></div>', 520), '', """# PermissionMode

Режим разрешения инструмента бота. Режимов четыре, пятый вид показывает неотключаемое подтверждение.

## Режимы
| Режим | Иконка | Токен | Что значит |
| --- | --- | --- | --- |
| Без вопроса | галочка | `success-fg` | бот делает сам, действие видно в ленте |
| Спросить | щит | `attention-fg` | перед каждым действием приходит `ApprovalCard` |
| По команде | реплика | `fg-muted` | только если попросить об этом прямо в сообщении |
| Запрещено | перечёркнутый круг | `danger-fg` | инструмент выключен, бот говорит, что не может |
| Нельзя снять | замок | `attention-fg` | пароли, оплата, удаление данных, установка программ |

## Правила
- Телефон: кнопка-плашка с текущим режимом открывает `Sheet` с четырьмя радиокнопками и пояснениями. Mac: `SegmentedControl` в строке инструмента.
- Режим всегда словом, цвет только у иконки.
- Действие, которое не удалось распознать, требует подтверждения при любом режиме.
- Инструмент назван по-человечески («Терминал», «Загрузка страниц»); техническое имя (Bash, WebFetch) только под раскрытием «Подробнее».
- Неотключаемый список показывается отдельной карточкой с объяснением: эти действия необратимы или открывают доступ к деньгам и аккаунтам, поэтому ни режим «Без вопроса», ни правило «Разрешать без спроса» их не покрывают.

## Доступность
- Плашка: `<button aria-haspopup="dialog">`. В листе `role="radiogroup"`.
""")

os.makedirs(ROOT, exist_ok=True)
for name,(group,h,body,css,readme) in C.items():
    d = os.path.join(ROOT,'components',name); os.makedirs(d, exist_ok=True)
    open(os.path.join(d,'preview.html'),'w').write(page(f'@dsCard group="{group}" height={h}', body, css))
    open(os.path.join(d,'README.md'),'w').write(readme)
print('components:', len(C))
