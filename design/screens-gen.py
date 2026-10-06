import json, os, datetime, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import avatars
HERE=os.path.dirname(os.path.abspath(__file__)); OUT=os.path.join(HERE,'screens')
T=json.load(open(os.path.join(HERE,'system','tokens.json')))
def varsblock(theme):
    out=[]
    for t in T['color']['tokens']:
        v=t['value']; v=v if isinstance(v,str) else v.get(theme, v['light'])
        out.append(f"--{t['name']}:{v}")
    return ';'.join(out)
STATIC=';'.join([f"--{t['name']}:{t['value']}" for fam in ('spacing','radius','size') for t in T[fam]['tokens']])
FONTS='<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Unbounded:wght@600&amp;family=Golos+Text:wght@400;500;600&amp;family=JetBrains+Mono:wght@400;500&amp;display=swap">'
HELMET=f"""<helmet>
{FONTS}
<style>
body{{margin:0}}
[data-theme="light"]{{{varsblock('light')};{STATIC}}}
[data-theme="dark"]{{{varsblock('dark')};{STATIC}}}
a{{color:var(--focus)}}a:hover{{opacity:.85}}
</style>
</helmet>"""
SANS="'Golos Text', -apple-system, system-ui, sans-serif"; DISP="'Unbounded', 'Golos Text', sans-serif"; MONO="'JetBrains Mono', ui-monospace, Menlo, monospace"
IC={'plus':'<path d="M12 5v14M5 12h14"></path>','send':'<path d="M12 19V5M5 12l7-7 7 7"></path>','stop':'<rect x="7" y="7" width="10" height="10" rx="2"></rect>','mic':'<rect x="9" y="3" width="6" height="11" rx="3"></rect><path d="M5 11a7 7 0 0014 0M12 18v3"></path>','screen':'<rect x="3" y="4" width="18" height="12" rx="2"></rect><path d="M8 20h8M12 16v4"></path>','risk':'<path d="M12 3l9 16H3z"></path><path d="M12 10v4M12 17h.01"></path>','check':'<path d="M5 12l5 5 9-10"></path>','x':'<path d="M6 6l12 12M18 6L6 18"></path>','chev':'<path d="M9 6l6 6-6 6"></path>','back':'<path d="M15 6l-6 6 6 6"></path>','down':'<path d="M6 9l6 6 6-6"></path>','bot':'<rect x="4" y="7" width="16" height="12" rx="3"></rect><path d="M12 3v4M9 13h.01M15 13h.01"></path>','clock':'<circle cx="12" cy="12" r="9"></circle><path d="M12 7v5l3 2"></path>','shield':'<path d="M12 3l8 4v5c0 5-3.5 8-8 9-4.5-1-8-4-8-9V7z"></path><path d="M9 12l2 2 4-4"></path>','bars':'<path d="M5 20V10M12 20V4M19 20v-7"></path>','play':'<path d="M8 5l11 7-11 7z"></path>','file':'<path d="M14 3H6v18h12V7z"></path><path d="M14 3v4h4"></path>','laptop':'<rect x="4" y="5" width="16" height="11" rx="2"></rect><path d="M2 19h20"></path>','search':'<circle cx="11" cy="11" r="7"></circle><path d="M20 20l-4-4"></path>','hand':'<path d="M8 13V5a1.5 1.5 0 013 0v6M11 11V4a1.5 1.5 0 013 0v7M14 11V5.5a1.5 1.5 0 013 0V14c0 4-2.5 7-6.5 7S5 18 4.5 15L3 11.5a1.5 1.5 0 012.6-1.4L8 13"></path>','bolt':'<path d="M13 2L4 14h7l-1 8 9-12h-7z"></path>','folder':'<path d="M3 6h6l2 2h10v11H3z"></path>','brain':'<path d="M12 4a4 4 0 00-4 4 4 4 0 00-2 7 4 4 0 006 4 4 4 0 006-4 4 4 0 00-2-7 4 4 0 00-4-4z"></path><path d="M12 4v16"></path>','gear':'<circle cx="12" cy="12" r="3"></circle><path d="M12 2v3M12 19v3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1M2 12h3M19 12h3M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1"></path>','list':'<path d="M9 6h11M9 12h11M9 18h11M4 6h.01M4 12h.01M4 18h.01"></path>','undo':'<path d="M9 14L4 9l5-5"></path><path d="M4 9h10a6 6 0 010 12h-3"></path>','eye':'<path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12z"></path><circle cx="12" cy="12" r="3"></circle>','download':'<path d="M12 4v11M7 10l5 5 5-5M5 20h14"></path>'}
def ic(n,s=20,w=2): return f'<svg width="{s}" height="{s}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="{w}" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{IC[n]}</svg>'
PROV={'claude':'claude','codex':'codex','gemini':'gemini'}
def badge(p,text): return f'<span style="display: inline-flex; align-items: center; padding: 2px 7px; border-radius: 6px; background: var(--{p}-bg); color: var(--{p}-fg); font: 600 12px/16px {SANS}; letter-spacing: 0.02em; white-space: nowrap;">{text}</span>'
KIND={'С':'scout','М':'mac','S':'sre','К':'coder','А':'archive'}
def avatar(p,letter,size=44): return avatars.avatar_html(KIND.get(letter,'robot'),p,size)
def dot(c): return f'<span style="width: 8px; height: 8px; border-radius: 4px; background: var(--{c}); display: inline-block; flex-shrink: 0;"></span>'
def status(c,t): return f'<span style="display: flex; align-items: center; gap: 6px;">{dot(c)}{t}</span>'
def btn(kind,text,icon=None,href=None,extra=''):
    st={'primary':'background: var(--bg-emphasis); color: var(--fg-on-emphasis); border: 1px solid transparent;','secondary':'background: var(--bg-surface); color: var(--fg-default); border: 1px solid var(--border-control);','ghost':'background: transparent; color: var(--fg-default); border: 1px solid transparent;','approve':'background: var(--attention-emphasis); color: var(--fg-on-attention); border: 1px solid transparent;','danger':'background: var(--danger-bg); color: var(--danger-fg); border: 1px solid transparent;'}[kind]
    inner=(ic(icon,16,2.4) if icon else '')+text
    s=f'min-height: 44px; padding: 0 16px; border-radius: 12px; {st} font: 500 15px {SANS}; display: inline-flex; align-items: center; justify-content: center; gap: 8px; text-decoration: none; box-sizing: border-box; {extra}'
    return f'<a href="{href}" style="{s}">{inner}</a>' if href else f'<button type="button" style="{s}">{inner}</button>'
def iconbtn(icon,label,kind='secondary',href=None,round_=False):
    st={'primary':'background: var(--bg-emphasis); color: var(--fg-on-emphasis); border: none;','secondary':'background: var(--bg-sunken); color: var(--fg-default); border: none;','ghost':'background: transparent; color: var(--fg-default); border: none;'}[kind]
    s=f'width: 44px; height: 44px; flex-shrink: 0; border-radius: {22 if round_ else 12}px; {st} display: flex; align-items: center; justify-content: center; text-decoration: none;'
    return f'<a href="{href}" aria-label="{label}" style="{s}">{ic(icon)}</a>' if href else f'<button type="button" aria-label="{label}" style="{s}">{ic(icon)}</button>'
CARD='background: var(--bg-surface); border: 1px solid var(--border-default); border-radius: 16px;'
FN=f'font: 400 13px/18px {SANS}; color: var(--fg-muted);'
def header_root(title, action=''):
    return f'<div style="padding: 20px 16px 12px; display: flex; align-items: center; justify-content: space-between; gap: 12px;"><h1 style="margin: 0; font: 600 30px/36px {DISP}; letter-spacing: -0.02em; color: var(--fg-default);">{title}</h1>{action}</div>'
def header_back(back, title, sub, p=None, letter=None, right=''):
    av=avatar(p,letter,36) if p else ''
    return f'<header style="padding: 12px 12px 10px; display: flex; align-items: center; gap: 8px; background: var(--bg-glass); border-bottom: 1px solid var(--border-default); backdrop-filter: blur(20px);">{iconbtn("back","Назад","ghost",back)}{av}<div style="flex-grow: 1; min-width: 0; display: flex; flex-direction: column;"><span style="font: 600 17px/22px {SANS}; color: var(--fg-default);">{title}</span><span style="{FN} white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">{sub}</span></div>{right}</header>'
TABS=[('bot','Боты','Main.dc.html'),('clock','Рутины','Routines.dc.html'),('shield','Решения','Approval.dc.html'),('bars','Расход','Usage.dc.html')]
def tabbar(active):
    items=''
    for i,(icn,t,h) in enumerate(TABS):
        a=i==active; badge_dot=f'<span aria-label="есть ожидающие" style="position: absolute; top: 4px; right: 30%; width: 8px; height: 8px; border-radius: 4px; background: var(--attention-fg);"></span>' if t=='Решения' else ''
        items+=f'<a href="{h}"{" aria-current=\"page\"" if a else ""} style="position: relative; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 3px; min-height: 48px; text-decoration: none; color: var(--{"fg-default" if a else "fg-muted"}); font: 600 11px/14px {SANS};">{ic(icn,22)}{t}{badge_dot}</a>'
    return f'<nav aria-label="Разделы" style="display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); padding: 6px 8px 22px; background: var(--bg-glass); border-top: 1px solid var(--border-default); backdrop-filter: blur(20px);">{items}</nav>'
def composer(bot, running=None):
    if running:
        return f'<div style="padding: 10px 12px 26px; background: var(--bg-surface); border-top: 1px solid var(--border-default); display: flex; align-items: center; gap: 8px;"><span style="flex-grow: 1; font: 500 15px {SANS}; color: var(--fg-muted);">{running}</span>{btn("danger","Стоп","stop")}</div>'
    return f'<div style="padding: 10px 12px 26px; background: var(--bg-surface); border-top: 1px solid var(--border-default); display: flex; flex-direction: column; gap: 8px;"><div style="display: flex; align-items: center; gap: 10px;">{bot}<label style="display: flex; align-items: center; gap: 6px; {FN}"><input type="checkbox" style="width: 18px; height: 18px; margin: 0;">Пробный прогон</label></div><div style="display: flex; align-items: center; gap: 8px;"><label for="msg" style="position: absolute; width: 1px; height: 1px; overflow: hidden;">Сообщение</label><input id="msg" type="text" placeholder="Сообщение" style="flex-grow: 1; min-width: 0; height: 44px; box-sizing: border-box; padding: 0 16px; border-radius: 22px; border: 1px solid var(--border-control); background: var(--bg-sunken); color: var(--fg-default); font: 400 15px {SANS};">{iconbtn("mic","Голосовой ввод","ghost",None,True)}{iconbtn("send","Отправить","primary",None,True)}</div></div>'
def model_pick(p,text): return f'<button type="button" style="min-height: 32px; padding: 0 10px; border-radius: 8px; border: none; background: var(--{p}-bg); color: var(--{p}-fg); font: 600 12px {SANS}; display: inline-flex; align-items: center; gap: 4px;">{text}{ic("down",12,2.6)}</button>'
def sysnote(t): return f'<div style="align-self: center; padding: 4px 10px; border-radius: 10px; background: var(--bg-sunken); {FN} text-align: center;">{t}</div>'
def owner_msg(t): return f'<div style="align-self: flex-end; max-width: 290px; padding: 12px 14px; border-radius: 16px 16px 4px 16px; background: var(--bg-emphasis); color: var(--fg-on-emphasis); font: 400 17px/24px {SANS};">{t}</div>'
def bot_msg(t, foot=''):
    f=f'<div style="display: flex; flex-wrap: wrap; gap: 10px; align-items: center; margin-top: 8px; font: 400 12px/16px {MONO}; color: var(--fg-muted);">{foot}</div>' if foot else ''
    return f'<div style="max-width: 330px; padding: 12px 14px; {CARD} border-radius: 16px 16px 16px 4px; color: var(--fg-default); font: 400 17px/24px {SANS};">{t}{f}</div>'
def plan_line(t): return f'<div style="display: flex; align-items: center; gap: 8px; padding: 10px 12px; {CARD} border-radius: 12px; font: 600 13px/18px {SANS}; color: var(--fg-muted);"><span style="color: var(--success-fg); display: flex;">{ic("check",16,2.6)}</span><span style="flex-grow: 1;">{t}</span><span style="display: inline-flex; align-items: center; gap: 4px; padding: 2px 7px; border-radius: 6px; background: var(--bg-sunken); color: var(--success-fg); font: 600 12px {SANS};">{ic("shield",12,2.4)}проверено</span></div>'
def page(name,title,body,w=390,h=844,interactive=True):
    root=f'<div data-theme="{{{{theme}}}}" style="width: {w}px; height: {h}px; box-sizing: border-box; overflow: hidden; background: var(--bg-canvas); color: var(--fg-default); font-family: {SANS}; display: flex; flex-direction: {"column" if w<600 else "row"};">{body}</div>'
    props=json.dumps({"theme":{"editor":"enum","options":["light","dark"],"default":"light","section":"Вид"},"$preview":{"width":w,"height":h}},ensure_ascii=False).replace("'","&#39;")
    html=f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>{title}</title>
<script src="./support.js"></script>
</head>
<body>
<x-dc>
{HELMET}
{root}
</x-dc>
<script type="text/x-dc" data-dc-script data-props='{props}'>
class Component extends DCLogic {{
renderVals() {{
return {{ theme: this.props.theme ?? 'light' }};
}}
}}
</script>
</body>
</html>
"""
    open(os.path.join(OUT,name),'w').write(html)
    return (name,title,w,h,interactive)
BOARDS=[]
SCROLL='flex-grow: 1; min-height: 0; overflow: hidden; padding: 16px; display: flex; flex-direction: column; gap: 12px;'

# 1 Main
def botcard(p,l,name,model,last,st,where,href='Thread.dc.html'):
    return f'<a href="{href}" style="display: flex; gap: 12px; padding: 14px; {CARD} text-decoration: none; color: var(--fg-default);">{avatar(p,l)}<span style="flex-grow: 1; min-width: 0; display: flex; flex-direction: column; gap: 4px;"><span style="display: flex; align-items: center; gap: 8px;"><span style="font: 600 17px/22px {SANS};">{name}</span>{badge(p,model)}</span><span style="{FN} white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">{last}</span><span style="display: flex; gap: 12px; {FN}">{st}<span>{where}</span></span></span></a>'
mac=f'<a href="BotSettings.dc.html" style="display: flex; align-items: center; gap: 12px; padding: 12px 14px; {CARD} text-decoration: none; color: var(--fg-default);"><span style="color: var(--fg-default); display: flex;">{ic("laptop",22)}</span><span style="flex-grow: 1; display: flex; flex-direction: column;"><span style="font: 600 15px/20px {SANS};">MacBook Air</span><span style="{FN}">Полный контроль · Tailscale · 21 мс</span></span><span style="font: 400 13px {SANS}; color: var(--fg-default);">{status("success-fg","В сети")}</span></a>'
banner=f'<a href="Approval.dc.html" style="display: flex; align-items: center; gap: 12px; padding: 12px 14px; border-radius: 14px; background: var(--attention-bg); border: 1px solid var(--attention-border); text-decoration: none; color: var(--attention-text); font: 600 15px {SANS};">{ic("risk",20)}<span style="flex-grow: 1;">2 действия ждут решения</span>{ic("chev",16,2.2)}</a>'
cards=''.join([
 botcard('gemini','С','Скаут','Gemini 3.1 Pro','Нашёл 7 вакансий SRE, жду решения по отклику',status('attention-fg','Ждёт тебя'),'Сервер','Approval.dc.html'),
 botcard('claude','М','Мак','Sonnet 5','Нашёл 3 файла: договор аренды 2025',status('success-fg','Готово'),'Mac','Thread.dc.html'),
 botcard('claude','S','SRE','Opus 5.5','Инцидент: 502 на webapp, 2 гипотезы',status('success-fg','Работает'),'Сервер','Incident.dc.html'),
 botcard('codex','К','Кодер','GPT-6 Sol','Остановлен предохранителем: 3 одинаковые ошибки',status('danger-fg','Стоп'),'Сервер','Usage.dc.html'),
 botcard('gemini','А','Архив','Gemini 3.8 Flash','Ждёт файлы в ~/BotHub/inbox',status('neutral-fg','Ждёт события'),'Mac','Routines.dc.html')])
BOARDS.append(page('Main.dc.html','Боты',header_root('Боты',f'<span style="display: flex; gap: 8px;">{iconbtn("gear","Настройки","secondary","Settings.dc.html")}{btn("primary","Бот","plus","NewBotDescribe.dc.html")}</span>')+f'<div style="{SCROLL} padding-top: 4px;">{mac}{banner}{cards}</div>'+tabbar(0)))

# 2 Thread: Mac file search
def filecard(name,path,meta):
    return f'<div style="{CARD} padding: 12px; display: flex; flex-direction: column; gap: 10px;"><div style="display: flex; gap: 12px; align-items: center;"><span style="width: 40px; height: 48px; flex-shrink: 0; border-radius: 8px; background: var(--danger-bg); color: var(--danger-fg); display: flex; align-items: center; justify-content: center; font: 600 11px {MONO};">PDF</span><span style="min-width: 0; display: flex; flex-direction: column; gap: 2px;"><span style="font: 600 15px/20px {SANS}; color: var(--fg-default);">{name}</span><span style="font: 400 12px/16px {MONO}; color: var(--fg-muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">{path}</span><span style="{FN}">{meta}</span></span></div><div style="display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px;">{btn("secondary","Превью","eye")}{btn("secondary","В тред")}{btn("secondary","Скачать","download")}</div></div>'
body=header_back('Main.dc.html','Мак','Claude Sonnet 5 · MacBook Air','claude','М',iconbtn('screen','Экран Mac','secondary','Handoff.dc.html'))
body+=f'<div style="{SCROLL}">'+sysnote('Начато на iPhone · 09:41')+owner_msg('Найди на маке PDF с договором аренды за 2025')+sysnote('Mac спал, разбудил по сети · 11 с')+plan_line('3 шага · 14 с · Spotlight, QuickLook')+bot_msg('Нашёл 3 файла. Самый свежий изменён 12 марта.')+filecard('Договор аренды 2025.pdf','~/Documents/Квартира/','1,2 МБ · изменён 12 мар 2025')+filecard('Договор аренды 2025 (скан).pdf','~/Downloads/','4,8 МБ · изменён 3 янв 2025')+'</div>'
body+=composer(model_pick('claude','Sonnet 5'))
BOARDS.append(page('Thread.dc.html','Поиск файла на Mac',body))

# 3 Approval sheet
dl=''.join(f'<dt style="{FN} color: var(--attention-text);">{a}</dt><dd style="margin: 0; font: 400 15px/20px {SANS}; color: var(--fg-default);">{b}</dd>' for a,b in [('Куда','jobs.example.eu/812'),('Данные','Резюме EN, email'),('Отменить','Нельзя')])
sheet=f'''<div style="flex-grow: 1; background: rgba(0, 0, 0, 0.35);"></div>
<section aria-labelledby="ap" style="background: var(--bg-surface); border-radius: 16px 16px 0 0; box-shadow: 0 -8px 24px rgba(22, 24, 26, 0.12); padding: 8px 16px 30px; display: flex; flex-direction: column; gap: 14px;">
<div style="align-self: center; width: 36px; height: 5px; border-radius: 3px; background: var(--border-default);"></div>
<div style="display: flex; align-items: center; gap: 10px;">{avatar("gemini","С",36)}<div style="flex-grow: 1; display: flex; flex-direction: column;"><h2 id="ap" style="margin: 0; font: 600 20px/26px {DISP}; color: var(--fg-default);">Подтверждение</h2><span style="{FN}">Скаут · 1 из 2 · истекает через 27 мин</span></div></div>
<div style="padding: 14px; border-radius: 16px; background: var(--attention-bg); border: 1px solid var(--attention-border); display: flex; flex-direction: column; gap: 12px;">
<div style="display: flex; align-items: center; gap: 6px; color: var(--attention-text); font: 600 12px/16px {SANS}; letter-spacing: 0.02em;">{ic("risk",16,2.2)}ОТПРАВКА</div>
<div style="font: 500 17px/22px {SANS}; color: var(--fg-default);">Отправить отклик на «Senior SRE, Belgrade/remote»</div>
<dl style="margin: 0; display: grid; grid-template-columns: auto 1fr; gap: 6px 14px; align-items: baseline;">{dl}</dl>
<details><summary style="min-height: 32px; display: flex; align-items: center; {FN} color: var(--attention-text); cursor: pointer;">Аргументы действия</summary><pre style="margin: 0; padding: 8px 10px; border-radius: 10px; background: var(--bg-surface); font: 400 12px/16px {MONO}; color: var(--fg-default); white-space: pre-wrap;">browser.submit_form
url: jobs.example.eu/812 · sha 4f2a…9c1</pre></details>
</div>
<label style="display: flex; gap: 10px; align-items: flex-start; font: 400 15px/20px {SANS}; color: var(--fg-default);"><input type="checkbox" style="width: 20px; height: 20px; margin: 0; flex-shrink: 0;"><span>Разрешать Скауту отклики на этом сайте без вопроса<br><span style="{FN}">Правило можно отключить в настройках бота</span></span></label>
<div style="display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px;">{btn("secondary","Отклонить")}{btn("secondary","Изменить")}{btn("approve","Разрешить")}</div>
</section>'''
BOARDS.append(page('Approval.dc.html','Подтверждение',sheet))

# 4 Handoff (screen + take control)
screen=f'''<div style="margin: 0 16px; border-radius: 16px; overflow: hidden; border: 1px solid var(--border-default); background: var(--bg-surface);">
<div style="height: 30px; display: flex; align-items: center; gap: 6px; padding: 0 10px; background: var(--bg-sunken); {FN} font-size: 12px;"><span style="width: 8px; height: 8px; border-radius: 4px; background: var(--border-control);"></span><span style="width: 8px; height: 8px; border-radius: 4px; background: var(--border-control);"></span><span style="margin-left: 8px; font-family: {MONO};">accounts.example.eu/verify</span></div>
<div style="height: 300px; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 14px; padding: 20px; background: var(--bg-canvas);">
<div style="font: 600 17px {SANS}; color: var(--fg-default);">Введите код из SMS</div>
<div style="display: flex; gap: 8px;">{"".join(f'<span style="width: 38px; height: 46px; border-radius: 10px; border: 2px solid var(--{"focus" if i==0 else "border-control"}); background: var(--bg-surface);"></span>' for i in range(6))}</div>
<div style="{FN}">[Экран бота: живой поток noVNC]</div></div></div>'''
body=header_back('Thread.dc.html','Экран · Скаут','Контейнер bot-scout · Chromium','gemini','С')
body+=f'<div style="padding: 12px 16px 0;"><div style="display: flex; align-items: center; gap: 10px; padding: 12px 14px; border-radius: 14px; background: var(--attention-bg); border: 1px solid var(--attention-border); color: var(--attention-text); font: 600 15px/20px {SANS};">{ic("hand",20)}<span>Бот ждёт тебя: код 2FA. Управление у тебя.</span></div></div>'
body+=f'<div style="height: 12px;"></div>{screen}'
body+=f'<div style="padding: 12px 16px; display: flex; flex-direction: column; gap: 6px; font: 400 12px/16px {MONO}; color: var(--fg-muted);"><span>09:52:10 browser.open accounts.example.eu</span><span>09:52:14 форма входа заполнена</span><span style="color: var(--attention-fg);">09:52:15 ждёт 2FA, управление передано владельцу</span></div>'
body+=f'<div style="flex-grow: 1;"></div><div style="padding: 10px 16px 28px; display: grid; grid-template-columns: 1fr auto; gap: 8px; background: var(--bg-surface); border-top: 1px solid var(--border-default);">{btn("primary","Вернуть боту","play","Thread.dc.html")}{btn("danger","Стоп","stop")}</div>'
BOARDS.append(page('Handoff.dc.html','Экран и передача управления',body))

# 5 Routines
def section(t): return f'<h2 style="margin: 8px 0 0; font: 600 12px/16px {SANS}; letter-spacing: 0.04em; text-transform: uppercase; color: var(--fg-muted);">{t}</h2>'
def routine(icon,title,sub,last,href='Procedure.dc.html',paused=False):
    return f'<div style="display: flex; align-items: center; gap: 12px; padding: 12px 14px; {CARD}"><span style="width: 40px; height: 40px; flex-shrink: 0; border-radius: 12px; background: var(--bg-sunken); color: var(--fg-default); display: flex; align-items: center; justify-content: center;">{ic(icon,20)}</span><a href="{href}" style="flex-grow: 1; min-width: 0; display: flex; flex-direction: column; gap: 2px; text-decoration: none; color: var(--fg-default);"><span style="font: 600 15px/20px {SANS};">{title}</span><span style="{FN} white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">{sub}</span><span style="{FN}">{last}</span></a>{iconbtn("play","Запустить сейчас","secondary") if not paused else btn("ghost","Пауза")}</div>'
body=header_root('Рутины',btn('secondary','Новая','plus'))
body+=f'<div style="{SCROLL} padding-top: 4px; gap: 10px;">'+section('По расписанию')+routine('clock','Проверка серверов','SRE · каждый день 03:00',f'<span style="display: flex; gap: 6px; align-items: center;"><span style="color: var(--success-fg); display: flex;">{ic("check",14,2.6)}</span>сегодня 03:00 · 2 мин · следующая через 17 ч</span>','Incident.dc.html')+routine('clock','Разбор почты','Секретарь · будни 08:30','на паузе с 20 сен',paused=True)
body+=section('По событию')+routine('bolt','Алерт → диагностика','SRE · webhook Alertmanager','последний: сегодня 09:12, 502 на webapp','Incident.dc.html')+routine('folder','Папка ~/BotHub/inbox','Архив · Mac · новый файл','вчера: 4 скана разложены по папкам','Routines.dc.html')
body+=section('Процедуры')+routine('list','Перевыпуск сертификата','4 шага · 1 подтверждение · из задачи 21 сен','3 успешных запуска','Procedure.dc.html')+'</div>'+tabbar(1)
BOARDS.append(page('Routines.dc.html','Рутины и процедуры',body))

# 6 Procedure
def pstep(n,t,appr=False):
    a=f'<span style="display: inline-flex; align-items: center; gap: 4px; padding: 2px 7px; border-radius: 6px; background: var(--attention-bg); color: var(--attention-text); font: 600 12px {SANS};">{ic("shield",12,2.4)}подтверждение</span>' if appr else ''
    return f'<li style="display: flex; gap: 12px; align-items: flex-start;"><span style="width: 24px; height: 24px; flex-shrink: 0; border-radius: 12px; background: var(--bg-sunken); color: var(--fg-default); display: flex; align-items: center; justify-content: center; font: 600 12px {MONO};">{n}</span><span style="display: flex; flex-direction: column; gap: 4px; font: 400 15px/20px {SANS}; color: var(--fg-default);">{t}{a}</span></li>'
def field(label,val,id_): return f'<div style="display: flex; flex-direction: column; gap: 6px;"><label for="{id_}" style="{FN}">{label}</label><input id="{id_}" type="text" value="{val}" style="height: 44px; box-sizing: border-box; padding: 0 14px; border-radius: 12px; border: 1px solid var(--border-control); background: var(--bg-sunken); color: var(--fg-default); font: 400 15px {MONO};"></div>'
body=header_back('Routines.dc.html','Перевыпуск сертификата','Процедура · SRE · создана из задачи 21 сен','claude','S')
body+=f'<div style="{SCROLL}"><div style="{CARD} padding: 14px; display: flex; flex-direction: column; gap: 12px;"><span style="font: 600 17px {SANS};">Параметры</span>{field("Домен","bots.example.com","p1")}{field("Сервер","server","p2")}</div>'
body+=f'<div style="{CARD} padding: 14px; display: flex; flex-direction: column; gap: 12px;"><span style="font: 600 17px {SANS};">Шаги</span><ol style="list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 12px;">{pstep(1,"Проверить срок сертификата и DNS")}{pstep(2,"certbot certonly --nginx для домена")}{pstep(3,"Перезагрузить nginx",True)}{pstep(4,"Проверить HTTPS и дату нового сертификата")}</ol></div>'
body+=f'<div style="{FN} padding: 0 4px;">3 успешных запуска · последний 21 сен · проверка: HTTP 200 и дата сертификата</div></div>'
body+=f'<div style="padding: 10px 16px 28px; display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; background: var(--bg-surface); border-top: 1px solid var(--border-default);">{btn("secondary","По расписанию","clock")}{btn("primary","Запустить","play")}</div>'
BOARDS.append(page('Procedure.dc.html','Процедура',body))

# 7 Incident
def tl(time,t,c='fg-muted'): return f'<li style="display: grid; grid-template-columns: 52px 1fr; gap: 10px; font: 400 14px/20px {SANS}; color: var(--fg-default);"><span style="font: 400 12px/20px {MONO}; color: var(--{c});">{time}</span><span>{t}</span></li>'
def hyp(title,ev,verdict,c):
    return f'<div style="{CARD} padding: 12px 14px; display: flex; flex-direction: column; gap: 6px;"><div style="display: flex; justify-content: space-between; gap: 8px; align-items: center;"><span style="font: 600 15px/20px {SANS};">{title}</span><span style="font: 600 12px {SANS}; color: var(--{c}); white-space: nowrap;">{verdict}</span></div><span style="font: 400 12px/16px {MONO}; color: var(--fg-muted);">{ev}</span></div>'
body=header_back('Main.dc.html','Инцидент · 502 webapp','SRE · Opus 5.5 · из алерта 09:12','claude','S')
body+=f'<div style="{SCROLL} gap: 10px;"><div style="display: flex; gap: 8px; flex-wrap: wrap;"><span style="padding: 4px 10px; border-radius: 6px; background: var(--danger-bg); color: var(--danger-fg); font: 600 12px {SANS};">КРИТИЧНО</span><span style="padding: 4px 10px; border-radius: 6px; background: var(--bg-sunken); color: var(--fg-muted); font: 600 12px {SANS};">2 мин сбора · 6 источников</span></div>'
body+=f'<ol style="list-style: none; margin: 0; padding: 12px 14px; {CARD} display: flex; flex-direction: column; gap: 6px;">{tl("09:12","Алерт: 502 на bots.example.com","danger-fg")}{tl("09:10","Контейнер webapp перезапускается в цикле")}{tl("09:08","Память контейнера достигла лимита 1 ГБ")}{tl("08:55","Деплой новой версии webapp")}</ol>'
body+=section('Гипотезы')+hyp('Не хватает памяти после деплоя','docker events: oom-kill ×4 · dmesg: Killed process','вероятно','attention-fg')+hyp('Кончился диск','df -h /: занято 41%','опровергнута','fg-muted')
body+=f'<div style="padding: 12px 14px; border-radius: 16px; background: var(--attention-bg); border: 1px solid var(--attention-border); display: flex; flex-direction: column; gap: 10px;"><span style="font: 500 15px/20px {SANS}; color: var(--fg-default);">Предлагаю: поднять лимит до 2 ГБ и перезапустить webapp</span><div style="display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px;">{btn("secondary","Отклонить")}{btn("approve","Разрешить")}</div></div></div>'
BOARDS.append(page('Incident.dc.html','Диагностика инцидента',body))

# 8 Usage
def meter(n,p,c,extra=''):
    return f'<div style="display: flex; align-items: center; gap: 10px; font: 400 13px {SANS};"><span style="width: 76px; font-weight: 600; color: var(--fg-default);">{n}</span><div role="meter" aria-label="{n}: {p}%" aria-valuenow="{p}" aria-valuemin="0" aria-valuemax="100" style="flex-grow: 1; height: 8px; border-radius: 4px; background: var(--bg-sunken); overflow: hidden;"><div style="width: {min(p,100)}%; height: 8px; border-radius: 4px; background: var(--{c});"></div></div><span style="width: 44px; text-align: right; font: 400 12px {MONO}; color: var(--{"danger-fg" if p>=100 else "fg-muted"});">{p}%</span></div>'
body=header_root('Расход')
body+=f'<div style="{SCROLL} padding-top: 4px;"><div style="{CARD} padding: 14px 16px; display: flex; flex-direction: column; gap: 10px;"><div style="display: flex; justify-content: space-between; {FN}"><span>Подписки, неделя</span><span>сброс пн 03:00</span></div>{meter("Claude",42,"claude-fg")}{meter("Codex",18,"codex-fg")}{meter("Gemini",7,"gemini-fg")}</div>'
body+=f'<div style="padding: 14px; border-radius: 16px; background: var(--danger-bg); color: var(--danger-fg); display: flex; flex-direction: column; gap: 10px;"><div style="display: flex; gap: 8px; align-items: flex-start; font: 600 15px/20px {SANS};">{ic("stop",18,2.4)}<span>Кодер остановлен предохранителем: 3 раза одна ошибка</span></div><code style="font: 400 12px/16px {MONO}; color: var(--fg-default);">npm ERR! ERESOLVE unable to resolve dependency tree</code><div style="display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px;">{btn("secondary","Открыть тред")}{btn("secondary","Другая модель")}</div></div>'
body+=f'<div style="{CARD} padding: 14px 16px; display: flex; flex-direction: column; gap: 10px;"><div style="{FN}">Дневной бюджет ботов</div>{meter("Скаут",64,"gemini-fg")}{meter("Мак",12,"claude-fg")}{meter("SRE",35,"claude-fg")}{meter("Кодер",100,"danger-fg")}{meter("Архив",5,"gemini-fg")}</div></div>'+tabbar(3)
BOARDS.append(page('Usage.dc.html','Расход и предохранитель',body))

# 9 Memory
def fact(t,src,act=True):
    right=f'<button type="button" aria-label="Изменить" style="width: 44px; height: 44px; border: none; background: transparent; color: var(--fg-muted); font: 600 18px {SANS};">···</button>'
    return f'<div style="display: flex; gap: 10px; align-items: center; padding: 10px 12px 10px 14px; {CARD}"><span style="flex-grow: 1; display: flex; flex-direction: column; gap: 2px;"><span style="font: 400 15px/20px {SANS}; color: var(--fg-default);">{t}</span><span style="{FN} font-size: 12px;">{src}</span></span>{right}</div>'
def proposed(t,src):
    return f'<div style="padding: 12px 14px; {CARD} display: flex; flex-direction: column; gap: 10px;"><span style="font: 400 15px/20px {SANS};">{t}</span><span style="{FN} font-size: 12px;">{src}</span><div style="display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px;">{btn("secondary","Не запоминать")}{btn("primary","Запомнить")}</div></div>'
body=header_back('Main.dc.html','Память','Общая для всех ботов · 38 фактов')
body+=f'<div style="{SCROLL} gap: 10px;"><div style="display: flex; align-items: center; gap: 8px; height: 44px; padding: 0 14px; border-radius: 12px; background: var(--bg-sunken); color: var(--fg-muted);">{ic("search",18)}<label for="ms" style="position: absolute; width: 1px; height: 1px; overflow: hidden;">Поиск по памяти</label><input id="ms" type="text" placeholder="Поиск по памяти" style="flex-grow: 1; border: none; background: transparent; font: 400 15px {SANS}; color: var(--fg-default);"></div>'
body+=section('Бот предлагает запомнить')+proposed('Резюме EN для откликов лежит в ~/Documents/CV/Resume_EN.pdf','Скаут · из задачи сегодня 09:50')
body+=section('Факты')+fact('server: диск 280 ГБ, Ubuntu, Docker','SRE · 13 сен · бессрочно')+fact('Договоры по квартире: ~/Documents/Квартира','Мак · сегодня · бессрочно')+fact('Релокация: сначала Сербия, потом Япония','ты · 11 сен · до пересмотра')+fact('Сертификаты: certbot --nginx, без wildcard','SRE · 21 сен · бессрочно')+'</div>'
BOARDS.append(page('Memory.dc.html','Память',body))

# 10 Bot settings
def seg(opts,active):
    return f'<div role="radiogroup" style="display: grid; grid-template-columns: repeat({len(opts)}, minmax(0, 1fr)); gap: 4px; padding: 4px; border-radius: 12px; background: var(--bg-sunken);">'+''.join(f'<button type="button" role="radio" aria-checked="{"true" if i==active else "false"}" style="min-height: 40px; border-radius: 9px; border: none; background: {"var(--bg-surface)" if i==active else "transparent"}; color: var(--fg-default); font: {"600" if i==active else "500"} 14px {SANS};">{o}</button>' for i,o in enumerate(opts))+'</div>'
def toggle(label,sub,on,id_):
    return f'<div style="display: flex; align-items: center; gap: 12px; min-height: 44px;"><label for="{id_}" style="flex-grow: 1; display: flex; flex-direction: column; gap: 2px;"><span style="font: 400 15px/20px {SANS}; color: var(--fg-default);">{label}</span><span style="{FN} font-size: 12px;">{sub}</span></label><input id="{id_}" type="checkbox" role="switch" {"checked" if on else ""} style="width: 44px; height: 26px; margin: 0; accent-color: var(--bg-emphasis);"></div>'
def group(title,inner): return f'<div style="{CARD} padding: 14px; display: flex; flex-direction: column; gap: 10px;"><span style="font: 600 15px {SANS}; color: var(--fg-default);">{title}</span>{inner}</div>'
body=header_back('Main.dc.html','Мак','Настройки бота','claude','М')
body+=f'<div style="{SCROLL} gap: 10px;">'
pick=''.join(f'<button type="button" aria-label="Персонаж: {n}" aria-pressed="{"true" if k=="mac" else "false"}" style="width: 40px; height: 40px; padding: 0; border-radius: 12px; border: 2px solid {"var(--fg-default)" if k=="mac" else "transparent"}; background: transparent; display: flex; align-items: center; justify-content: center;">{avatars.avatar_svg(k,32)}</button>' for k,n in [('mac','Мак'),('scout','Скаут'),('sre','SRE'),('coder','Кодер'),('archive','Архив'),('owl','Сова'),('spark','Искра'),('robot','Робот')])
body+=f'<div style="{CARD} padding: 14px; display: flex; flex-direction: column; gap: 12px;"><div style="display: flex; align-items: center; gap: 14px;">{avatars.avatar_html("mac","claude",64)}<div style="flex-grow: 1; display: flex; flex-direction: column; gap: 2px;"><span style="font: 600 17px/22px {SANS};">Мак</span><span style="{FN}">Ищет файлы, открывает приложения, делает скриншоты</span></div>{btn("secondary","Другой")}</div><div style="display: flex; justify-content: space-between;">{pick}</div></div>'
body+=group('Модель',seg(['Claude','Codex','Gemini'],0)+f'<button type="button" style="min-height: 44px; display: flex; align-items: center; justify-content: space-between; padding: 0 14px; border-radius: 12px; border: 1px solid var(--border-control); background: var(--bg-surface); color: var(--fg-default); font: 500 15px {SANS};">Sonnet 5{ic("down",16,2.4)}</button>')
body+=group('Где работает',seg(['Сервер','Mac'],1)+toggle('Полный контроль Mac','Файлы, приложения, клики и ввод, скриншоты',True,'t1'))
body+=group('Правила',f'<span style="{FN}">Без вопроса: чтение файлов, Spotlight, открыть приложение, скриншот</span><span style="{FN} color: var(--attention-text);">Всегда спрашивать: удаление, отправка, оплата, вход</span><span style="{FN}">Предохранитель: 200k токенов в день, стоп после 3 одинаковых ошибок, до 30 мин на задачу</span>')
body+='</div>'
BOARDS.append(page('BotSettings.dc.html','Настройки бота',body))

# 11 Desktop
def side_item(p,l,name,st,active=False):
    return f'<a href="Thread.dc.html" style="display: flex; gap: 10px; align-items: center; padding: 8px 10px; border-radius: 12px; background: {"var(--bg-surface)" if active else "transparent"}; {"border: 1px solid var(--border-default);" if active else "border: 1px solid transparent;"} text-decoration: none; color: var(--fg-default);">{avatar(p,l,32)}<span style="display: flex; flex-direction: column; min-width: 0;"><span style="font: 600 14px/18px {SANS};">{name}</span><span style="font: 400 12px/16px {SANS}; color: var(--fg-muted);">{st}</span></span></a>'
side=f'<aside style="width: 280px; flex-shrink: 0; padding: 20px 16px; display: flex; flex-direction: column; gap: 6px; border-right: 1px solid var(--border-default);"><div style="font: 600 20px/26px {DISP}; margin-bottom: 10px;">botstead</div>'+side_item('claude','S','SRE','Инцидент · работает',True)+side_item('claude','М','Мак','Готово')+side_item('gemini','С','Скаут','Ждёт тебя')+side_item('codex','К','Кодер','Стоп: предохранитель')+side_item('gemini','А','Архив','Ждёт события')+f'<div style="height: 12px;"></div>'+''.join(f'<a href="{h}" style="display: flex; gap: 10px; align-items: center; min-height: 40px; padding: 0 10px; border-radius: 10px; text-decoration: none; color: var(--fg-default); font: 500 14px {SANS};">{ic(i,18)}{t}</a>' for i,t,h in [('clock','Рутины','Routines.dc.html'),('shield','Решения · 2','Approval.dc.html'),('brain','Память','Memory.dc.html'),('bars','Расход','Usage.dc.html')])+'</aside>'
center=f'<main style="flex-grow: 1; min-width: 0; display: flex; flex-direction: column;"><div style="padding: 18px 24px; border-bottom: 1px solid var(--border-default); display: flex; align-items: center; gap: 12px;">{avatar("claude","S",36)}<div style="flex-grow: 1;"><div style="font: 600 17px {SANS};">SRE · Инцидент 502 webapp</div><div style="{FN}">Opus 5.5 · сервер · начато из алерта</div></div>{btn("secondary","Новая задача")}</div><div style="flex-grow: 1; padding: 20px 24px; display: flex; flex-direction: column; gap: 12px; overflow: hidden; max-width: 760px;">'+sysnote('Продолжено на Mac · 09:20')+plan_line('6 шагов · 2 мин · docker, dmesg, nginx')+bot_msg('Причина почти наверняка в памяти: после деплоя 08:55 контейнер упирается в лимит 1 ГБ, ядро убивает процесс 4 раза.','<span>58k токенов</span><span>2 мин 04 с</span>')+f'<div style="padding: 14px; border-radius: 16px; background: var(--attention-bg); border: 1px solid var(--attention-border); display: flex; align-items: center; gap: 12px; max-width: 560px;"><span style="flex-grow: 1; font: 500 15px/20px {SANS};">Поднять лимит до 2 ГБ и перезапустить webapp</span>{btn("secondary","Отклонить")}{btn("approve","Разрешить")}</div></div>'+f'<div style="padding: 12px 24px 20px; border-top: 1px solid var(--border-default); display: flex; gap: 8px; align-items: center;">{model_pick("claude","Opus 5.5")}<label for="dm" style="position: absolute; width: 1px; height: 1px; overflow: hidden;">Сообщение</label><input id="dm" type="text" placeholder="Сообщение SRE  ⌘↵ отправить" style="flex-grow: 1; height: 44px; box-sizing: border-box; padding: 0 16px; border-radius: 22px; border: 1px solid var(--border-control); background: var(--bg-sunken); color: var(--fg-default); font: 400 15px {SANS};">{iconbtn("send","Отправить","primary",None,True)}</div></main>'
right=f'<aside style="width: 360px; flex-shrink: 0; padding: 20px 16px; border-left: 1px solid var(--border-default); display: flex; flex-direction: column; gap: 12px;"><div style="{FN} font-weight: 600;">Экран бота</div><div style="height: 200px; border-radius: 12px; border: 1px solid var(--border-default); background: var(--bg-sunken); display: flex; align-items: center; justify-content: center; {FN} font-family: {MONO};">[терминал server · noVNC]</div>{btn("secondary","Перехватить","hand","Handoff.dc.html")}<div style="{FN} font-weight: 600; margin-top: 8px;">Шаги</div><div style="display: flex; flex-direction: column; gap: 6px; font: 400 12px/16px {MONO}; color: var(--fg-muted);"><span>✓ docker ps -a</span><span>✓ docker events --since 1h</span><span>✓ dmesg | grep -i kill</span><span>✓ df -h</span><span>✓ nginx error.log</span><span style="color: var(--attention-fg);">… ждёт решения</span></div><div style="{FN} font-weight: 600; margin-top: 8px;">Квоты</div>{meter("Claude",42,"claude-fg")}{meter("Codex",18,"codex-fg")}{meter("Gemini",7,"gemini-fg")}</aside>'
BOARDS.append(page('Desktop.dc.html','botstead на Mac',side+center+right,1440,900))

# canvas index
pos={}; x=0
row1=['Main.dc.html','Thread.dc.html','Approval.dc.html','Handoff.dc.html','Memory.dc.html','BotSettings.dc.html']
row2=['Routines.dc.html','Procedure.dc.html','Incident.dc.html','Usage.dc.html']
Y1,Y2,Y3=0,1264,2528
for i,n in enumerate(row1): pos[n]=(i*470,Y1)
for i,n in enumerate(row2): pos[n]=(i*470,Y2)
pos['Desktop.dc.html']=(0,Y3)
now=datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
boards={n:{"x":pos[n][0],"y":pos[n][1],"w":w,"h":h,"title":t,"is_interactive":True} for n,t,w,h,_ in BOARDS}
idx={"v":3,"createdOnFiles":{"v":1,"at":now},"title":"Bot Hub: экраны","launch":{"view":"canvas"},"pages":[],"boards":boards,"order":[b[0] for b in BOARDS],
 "notes":{"t1":{"x":0,"y":-300,"text":"Телефон: боты, Mac, решения, память","kind":"title1","maxW":2750},
          "t2":{"x":0,"y":Y2-300,"text":"Телефон: рутины, процедуры, инциденты, расход","kind":"title1","maxW":1800},
          "t3":{"x":0,"y":Y3-300,"text":"Mac: три колонки","kind":"title1","maxW":1440},
          "n1":{"x":1900,"y":Y2,"text":"Каждый экран: переключатель темы light/dark в Tweaks. Play проходит по ссылкам: Боты → Мак (поиск файла) → Экран → Вернуть боту; Решения → Подтверждение; Рутины → Процедура / Инцидент.","w":320,"fill":"yellow" if False else "blue"}},
 "designSystems":[{"title":"Bot Hub","namespace":"bothub","artifact":"https://claude.ai/artifact/SYnAK5PP5s9oVj85sws3cm","version":None,"copiedAt":now}]}
# canvas.json общий с proto-gen.py и screens2-gen.py: целиком пишем только если его ещё нет
if not os.path.exists(os.path.join(OUT,'canvas.json')): json.dump(idx,open(os.path.join(OUT,'canvas.json'),'w'),ensure_ascii=False,indent=1)
print(len(BOARDS),'boards')
