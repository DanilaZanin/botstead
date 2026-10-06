#!/usr/bin/env python3
"""Генератор кликабельного прототипа Bot Hub: design/screens/Prototype.dc.html.

Один интерактивный артборд 390x844: все экраны лежат в <sc-if> с общим state,
чтобы в режиме Play можно было реально пройтись по приложению.
Токены темы, иконки, кнопки и персонажи те же, что в screens-gen.py и avatars.py.
Заодно прописывает артборд в design/screens/canvas.json (идемпотентно).

Запуск: python3 design/proto-gen.py
"""
import datetime
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import avatars  # noqa: E402

OUT_DIR = os.path.join(HERE, 'screens')
OUT = os.path.join(OUT_DIR, 'Prototype.dc.html')
NAME = 'Prototype.dc.html'
W, HGT = 390, 844

for _p in (os.path.join(HERE, 'system', 'tokens.json'), os.path.join(HERE, '..', 'bothub-ds', 'project', 'tokens.json')):
    if os.path.exists(_p):
        T = json.load(open(_p))
        break
else:
    sys.exit('tokens.json не найден (design/system/tokens.json)')


def varsblock(theme):
    out = []
    for t in T['color']['tokens']:
        v = t['value']
        v = v if isinstance(v, str) else v.get(theme, v['light'])
        out.append(f"--{t['name']}:{v}")
    return ';'.join(out)


STATIC = ';'.join(f"--{t['name']}:{t['value']}" for fam in ('spacing', 'radius', 'size') for t in T[fam]['tokens'])
FONTS = '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Unbounded:wght@600&amp;family=Golos+Text:wght@400;500;600&amp;family=JetBrains+Mono:wght@400;500&amp;display=swap">'
HELMET = f"""<helmet>
{FONTS}
<style>
body{{margin:0}}
[data-theme="light"]{{{varsblock('light')};{STATIC}}}
[data-theme="dark"]{{{varsblock('dark')};{STATIC}}}
a{{color:var(--focus)}}a:hover{{opacity:.85}}
sc-if,sc-for{{display:contents}}
button{{font-family:inherit;cursor:pointer;-webkit-tap-highlight-color:transparent}}
input,textarea{{font-family:inherit}}
.scr::-webkit-scrollbar{{display:none}}
</style>
</helmet>"""

SANS = "'Golos Text', -apple-system, system-ui, sans-serif"
DISP = "'Unbounded', 'Golos Text', sans-serif"
MONO = "'JetBrains Mono', ui-monospace, Menlo, monospace"

IC = {'plus': '<path d="M12 5v14M5 12h14"></path>', 'send': '<path d="M12 19V5M5 12l7-7 7 7"></path>', 'stop': '<rect x="7" y="7" width="10" height="10" rx="2"></rect>', 'mic': '<rect x="9" y="3" width="6" height="11" rx="3"></rect><path d="M5 11a7 7 0 0014 0M12 18v3"></path>', 'screen': '<rect x="3" y="4" width="18" height="12" rx="2"></rect><path d="M8 20h8M12 16v4"></path>', 'risk': '<path d="M12 3l9 16H3z"></path><path d="M12 10v4M12 17h.01"></path>', 'check': '<path d="M5 12l5 5 9-10"></path>', 'x': '<path d="M6 6l12 12M18 6L6 18"></path>', 'chev': '<path d="M9 6l6 6-6 6"></path>', 'back': '<path d="M15 6l-6 6 6 6"></path>', 'down': '<path d="M6 9l6 6 6-6"></path>', 'bot': '<rect x="4" y="7" width="16" height="12" rx="3"></rect><path d="M12 3v4M9 13h.01M15 13h.01"></path>', 'clock': '<circle cx="12" cy="12" r="9"></circle><path d="M12 7v5l3 2"></path>', 'shield': '<path d="M12 3l8 4v5c0 5-3.5 8-8 9-4.5-1-8-4-8-9V7z"></path><path d="M9 12l2 2 4-4"></path>', 'bars': '<path d="M5 20V10M12 20V4M19 20v-7"></path>', 'play': '<path d="M8 5l11 7-11 7z"></path>', 'file': '<path d="M14 3H6v18h12V7z"></path><path d="M14 3v4h4"></path>', 'laptop': '<rect x="4" y="5" width="16" height="11" rx="2"></rect><path d="M2 19h20"></path>', 'search': '<circle cx="11" cy="11" r="7"></circle><path d="M20 20l-4-4"></path>', 'hand': '<path d="M8 13V5a1.5 1.5 0 013 0v6M11 11V4a1.5 1.5 0 013 0v7M14 11V5.5a1.5 1.5 0 013 0V14c0 4-2.5 7-6.5 7S5 18 4.5 15L3 11.5a1.5 1.5 0 012.6-1.4L8 13"></path>', 'bolt': '<path d="M13 2L4 14h7l-1 8 9-12h-7z"></path>', 'folder': '<path d="M3 6h6l2 2h10v11H3z"></path>', 'brain': '<path d="M12 4a4 4 0 00-4 4 4 4 0 00-2 7 4 4 0 006 4 4 4 0 006-4 4 4 0 00-2-7 4 4 0 00-4-4z"></path><path d="M12 4v16"></path>', 'gear': '<circle cx="12" cy="12" r="3"></circle><path d="M12 2v3M12 19v3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1M2 12h3M19 12h3M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1"></path>', 'list': '<path d="M9 6h11M9 12h11M9 18h11M4 6h.01M4 12h.01M4 18h.01"></path>', 'undo': '<path d="M9 14L4 9l5-5"></path><path d="M4 9h10a6 6 0 010 12h-3"></path>', 'eye': '<path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12z"></path><circle cx="12" cy="12" r="3"></circle>', 'download': '<path d="M12 4v11M7 10l5 5 5-5M5 20h14"></path>'}


def ic(n, s=20, w=2):
    return f'<svg width="{s}" height="{s}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="{w}" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{IC[n]}</svg>'


def H(p):
    """Дырка шаблона: {{путь}}."""
    return '{{' + p + '}}'


def sub(s):
    return s.replace('@SANS@', SANS).replace('@DISP@', DISP).replace('@MONO@', MONO).replace('@CARD@', CARD).replace('@FN@', FN)


FN = f'font: 400 13px/18px {SANS}; color: var(--fg-muted);'
CARD = 'background: var(--bg-surface); border: 1px solid var(--border-default); border-radius: 16px; flex-shrink: 0;'
COL = 'flex-grow: 1; min-height: 0; display: flex; flex-direction: column;'
SCROLL = 'flex-grow: 1; min-height: 0; overflow-y: auto; padding: 16px; display: flex; flex-direction: column; gap: 12px; scrollbar-width: none;'
RESET_BTN = 'background: transparent; border: none; padding: 0; margin: 0; text-align: left; color: var(--fg-default);'


def btn(kind, text, icon=None, on=None, extra='', disabled=None):
    st = {'primary': 'background: var(--bg-emphasis); color: var(--fg-on-emphasis); border: 1px solid transparent;', 'secondary': 'background: var(--bg-surface); color: var(--fg-default); border: 1px solid var(--border-control);', 'ghost': 'background: transparent; color: var(--fg-default); border: 1px solid transparent;', 'approve': 'background: var(--attention-emphasis); color: var(--fg-on-attention); border: 1px solid transparent;', 'danger': 'background: var(--danger-bg); color: var(--danger-fg); border: 1px solid transparent;'}[kind]
    inner = (ic(icon, 16, 2.4) if icon else '') + text
    s = f'min-height: 44px; padding: 0 16px; border-radius: 12px; {st} font: 500 15px {SANS}; display: inline-flex; align-items: center; justify-content: center; gap: 8px; box-sizing: border-box; {extra}'
    dis = f' disabled="{H(disabled)}"' if disabled else ''
    return f'<button type="button" onClick="{H(on)}"{dis} style="{s}">{inner}</button>'


def iconbtn(icon, label, kind, on, round_=False):
    st = {'primary': 'background: var(--bg-emphasis); color: var(--fg-on-emphasis); border: none;', 'secondary': 'background: var(--bg-sunken); color: var(--fg-default); border: none;', 'ghost': 'background: transparent; color: var(--fg-default); border: none;'}[kind]
    s = f'width: 44px; height: 44px; flex-shrink: 0; padding: 0; border-radius: {22 if round_ else 12}px; {st} display: flex; align-items: center; justify-content: center;'
    return f'<button type="button" aria-label="{label}" onClick="{H(on)}" style="{s}">{ic(icon)}</button>'


KINDS = [('scout', 'isScout'), ('mac', 'isMac'), ('sre', 'isSre'), ('coder', 'isCoder'), ('archive', 'isArchive'), ('owl', 'isOwl'), ('spark', 'isSpark'), ('robot', 'isRobot')]


def avatar_dyn(pref, size=44):
    """Персонаж бота из state: по <sc-if> на каждый из 8 персонажей, точка провайдера Claude."""
    inner = ''
    for k, flag in KINDS:
        inner += f'<sc-if value="{H(pref + "." + flag)}" hint-placeholder-val="{H("true" if k == "mac" else "false")}">{avatars.avatar_svg(k, size)}</sc-if>'
    d = max(10, round(size * 0.3))
    dot = f'<span title="claude" style="position: absolute; right: -2px; bottom: -2px; width: {d}px; height: {d}px; border-radius: {d}px; background: var(--claude-fg); border: 2px solid var(--bg-surface); box-sizing: border-box;"></span>'
    return f'<span aria-hidden="true" style="position: relative; display: inline-block; width: {size}px; height: {size}px; flex-shrink: 0;">{inner}{dot}</span>'


def badge(text):
    return f'<span style="display: inline-flex; align-items: center; padding: 2px 7px; border-radius: 6px; background: var(--claude-bg); color: var(--claude-fg); font: 600 12px/16px {SANS}; letter-spacing: 0.02em; white-space: nowrap;">{text}</span>'


def dot_dyn(color_path):
    return f'<span style="width: 8px; height: 8px; border-radius: 4px; background: {H(color_path)}; display: inline-block; flex-shrink: 0;"></span>'


def SIF(flag, inner, default=False):
    return f'<sc-if value="{H(flag)}" hint-placeholder-val="{H("true" if default else "false")}">{inner}</sc-if>'


def SFOR(lst, var, inner, n=3):
    return f'<sc-for list="{H(lst)}" as="{var}" hint-placeholder-count="{n}">{inner}</sc-for>'


def header_root(title, action=''):
    return f'<div style="padding: 20px 16px 12px; display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-shrink: 0;"><h1 style="margin: 0; font: 600 30px/36px {DISP}; letter-spacing: -0.02em; color: var(--fg-default);">{title}</h1><div style="display: flex; align-items: center; gap: 8px;">{action}</div></div>'


def header_back(title, subtitle, av='', right='', on='goBack'):
    ell = 'white-space: nowrap; overflow: hidden; text-overflow: ellipsis;'
    return f'<header style="padding: 12px 12px 10px; display: flex; align-items: center; gap: 8px; background: var(--bg-glass); border-bottom: 1px solid var(--border-default); backdrop-filter: blur(20px); flex-shrink: 0;">{iconbtn("back", "Назад", "ghost", on)}{av}<div style="flex-grow: 1; min-width: 0; display: flex; flex-direction: column;"><span style="font: 600 17px/22px {SANS}; color: var(--fg-default); {ell}">{title}</span><span style="{FN} {ell}">{subtitle}</span></div>{right}</header>'


def section(t):
    return f'<h2 style="margin: 8px 0 0; font: 600 12px/16px {SANS}; letter-spacing: 0.04em; text-transform: uppercase; color: var(--fg-muted); flex-shrink: 0;">{t}</h2>'


def seg_loop(lst, cols):
    """Переключатель из списка опций state: {label, checked, bg, weight, pick}."""
    btn_ = f'<button type="button" role="radio" aria-checked="{H("o.checked")}" onClick="{H("o.pick")}" style="min-height: 40px; border-radius: 9px; border: none; background: {H("o.bg")}; color: var(--fg-default); font: {H("o.weight")} 14px {SANS};">{H("o.label")}</button>'
    return f'<div role="radiogroup" style="display: grid; grid-template-columns: repeat({cols}, minmax(0, 1fr)); gap: 4px; padding: 4px; border-radius: 12px; background: var(--bg-sunken);">{SFOR(lst, "o", btn_, cols)}</div>'


def group(title, inner):
    return f'<div style="{CARD} padding: 14px; display: flex; flex-direction: column; gap: 10px;"><span style="font: 600 15px {SANS}; color: var(--fg-default);">{title}</span>{inner}</div>'


def meter_loop(lst):
    row = (f'<div style="display: flex; align-items: center; gap: 10px; font: 400 13px {SANS};"><span style="width: 76px; font-weight: 600; color: var(--fg-default); white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">{H("m.name")}</span>'
           f'<div role="meter" aria-label="{H("m.name")}" aria-valuenow="{H("m.pct")}" aria-valuemin="0" aria-valuemax="100" style="flex-grow: 1; height: 8px; border-radius: 4px; background: var(--bg-sunken); overflow: hidden;"><div style="width: {H("m.width")}; height: 8px; border-radius: 4px; background: {H("m.color")};"></div></div>'
           f'<span style="width: 44px; text-align: right; font: 400 12px {MONO}; color: {H("m.pctColor")};">{H("m.pctText")}</span></div>')
    return SFOR(lst, 'm', row, 4)


# ---------------------------------------------------------------- экраны

def screen_bots():
    mac = (f'<button type="button" onClick="{H("macCard.toggle")}" style="{RESET_BTN} width: 100%; box-sizing: border-box; display: flex; align-items: center; gap: 12px; padding: 12px 14px; {CARD}">'
           f'<span style="color: var(--fg-default); display: flex;">{ic("laptop", 22)}</span>'
           f'<span style="flex-grow: 1; min-width: 0; display: flex; flex-direction: column;"><span style="font: 600 15px/20px {SANS};">MacBook Air</span><span style="{FN}">{H("macCard.sub")}</span></span>'
           f'<span style="display: flex; align-items: center; gap: 6px; font: 400 13px {SANS}; color: var(--fg-default);">{dot_dyn("macCard.dot")}{H("macCard.label")}</span></button>')
    banner = SIF('bannerShow', f'<button type="button" onClick="{H("goApprovals")}" style="width: 100%; box-sizing: border-box; display: flex; align-items: center; gap: 12px; padding: 12px 14px; border-radius: 14px; background: var(--attention-bg); border: 1px solid var(--attention-border); text-align: left; color: var(--attention-text); font: 600 15px {SANS}; flex-shrink: 0;">{ic("risk", 20)}<span style="flex-grow: 1;">{H("bannerText")}</span>{ic("chev", 16, 2.2)}</button>')
    card = (f'<button type="button" onClick="{H("b.open")}" style="{RESET_BTN} width: 100%; box-sizing: border-box; display: flex; gap: 12px; padding: 14px; {CARD}">{avatar_dyn("b", 44)}'
            f'<span style="flex-grow: 1; min-width: 0; display: flex; flex-direction: column; gap: 4px;"><span style="display: flex; align-items: center; gap: 8px;"><span style="font: 600 17px/22px {SANS};">{H("b.name")}</span>{badge(H("b.modelLabel"))}</span>'
            f'<span style="{FN} white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">{H("b.last")}</span>'
            f'<span style="display: flex; gap: 12px; {FN}"><span style="display: flex; align-items: center; gap: 6px;">{dot_dyn("b.dotColor")}{H("b.statusText")}</span><span>{H("b.whereLabel")}</span></span></span></button>')
    action = iconbtn('brain', 'Память', 'secondary', 'openMemory') + iconbtn('gear', 'Настройки', 'secondary', 'openSettings') + btn('primary', 'Бот', 'plus', 'openNewBot')
    body = f'<div class="scr" style="{SCROLL} padding-top: 4px;">{mac}{banner}{SFOR("bots", "b", card, 5)}</div>'
    return SIF('isBots', f'<div style="{COL}">{header_root("Боты", action)}{body}</div>', True)


def screen_thread():
    right = iconbtn('screen', 'Экран бота', 'secondary', 'openHandoff') + iconbtn('gear', 'Настройки бота', 'secondary', 'openSettings')
    header = header_back(H('cur.name'), H('cur.sub'), avatar_dyn('cur', 36), right)
    owner = f'<div style="align-self: flex-end; max-width: 290px; padding: 12px 14px; border-radius: 16px 16px 4px 16px; background: var(--bg-emphasis); color: var(--fg-on-emphasis); font: 400 17px/24px {SANS}; flex-shrink: 0; overflow-wrap: anywhere;">{H("m.text")}</div>'
    sysn = f'<div style="align-self: center; padding: 4px 10px; border-radius: 10px; background: var(--bg-sunken); {FN} text-align: center; flex-shrink: 0;">{H("m.text")}</div>'
    foot = SIF('m.hasFoot', f'<div style="display: flex; flex-wrap: wrap; gap: 10px; align-items: center; margin-top: 8px; font: 400 12px/16px {MONO}; color: var(--fg-muted);"><span>{H("m.foot.tokens")}</span><span>{H("m.foot.secs")}</span><span>{H("m.foot.model")}</span></div>')
    botm = f'<div style="align-self: flex-start; max-width: 330px; padding: 12px 14px; {CARD} border-radius: 16px 16px 16px 4px; color: var(--fg-default); font: 400 17px/24px {SANS}; overflow-wrap: anywhere;">{H("m.text")}{foot}</div>'
    ap_btns = f'<div style="display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px;">{btn("secondary", "Отклонить", None, "m.apReject", "padding: 0 8px;")}{btn("approve", "Разрешить", None, "m.apApprove", "padding: 0 8px;")}</div>'
    link = f'<div style="align-self: flex-start; max-width: 330px; padding: 12px 14px; {CARD} border-radius: 16px 16px 16px 4px; color: var(--fg-default); font: 400 17px/24px {SANS}; display: flex; flex-direction: column; gap: 10px;"><span style="overflow-wrap: anywhere;">{H("m.text")}</span>{SIF("m.hasAp", ap_btns)}{btn("secondary", H("m.label"), "chev", "m.openLink")}</div>'
    mark_done = f'<span style="color: var(--success-fg); display: flex;">{ic("check", 14, 2.6)}</span>'
    mark_run = '<span style="width: 8px; height: 8px; border-radius: 4px; background: var(--attention-fg);"></span>'
    mark_wait = '<span style="width: 8px; height: 8px; border-radius: 4px; border: 1.5px solid var(--border-control); box-sizing: border-box;"></span>'
    step = (f'<li style="display: flex; align-items: center; gap: 8px; font: 400 13px/18px {SANS}; color: {H("s.color")};">'
            f'<span style="width: 16px; height: 16px; flex-shrink: 0; display: flex; align-items: center; justify-content: center;">'
            f'{SIF("s.isDone", mark_done)}{SIF("s.isRun", mark_run)}{SIF("s.isWait", mark_wait)}'
            f'</span><span>{H("s.t")}</span></li>')
    pill = f'display: inline-flex; align-items: center; gap: 4px; padding: 2px 7px; border-radius: 6px; background: var(--bg-sunken); font: 600 12px {SANS};'
    head_run = f'<span style="color: var(--attention-fg); display: flex;">{ic("clock", 16, 2.4)}</span>'
    head_done = f'<span style="color: var(--success-fg); display: flex;">{ic("check", 16, 2.6)}</span>'
    head_stop = f'<span style="color: var(--danger-fg); display: flex;">{ic("stop", 16, 2.4)}</span>'
    pill_ok = f'<span style="{pill} color: var(--success-fg);">{ic("shield", 12, 2.4)}проверено</span>'
    pill_dry = f'<span style="{pill} color: var(--fg-muted);">{ic("eye", 12, 2.4)}пробный</span>'
    plan = (f'<div style="{CARD} border-radius: 12px; padding: 10px 12px; display: flex; flex-direction: column; gap: 8px;">'
            f'<div style="display: flex; align-items: center; gap: 8px; font: 600 13px/18px {SANS}; color: var(--fg-muted);">'
            f'{SIF("m.planRun", head_run)}{SIF("m.planDone", head_done)}{SIF("m.planStopped", head_stop)}'
            f'<span style="flex-grow: 1;">{H("m.planTitle")}</span>{SIF("m.planVerified", pill_ok)}{SIF("m.planDry", pill_dry)}'
            f'</div><ol style="list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 6px;">{SFOR("m.steps", "s", step, 3)}</ol></div>')
    fcard = (f'<div style="{CARD} padding: 12px; display: flex; flex-direction: column; gap: 10px;"><div style="display: flex; gap: 12px; align-items: center;">'
             f'<span style="width: 40px; height: 48px; flex-shrink: 0; border-radius: 8px; background: var(--danger-bg); color: var(--danger-fg); display: flex; align-items: center; justify-content: center; font: 600 11px {MONO};">PDF</span>'
             f'<span style="min-width: 0; display: flex; flex-direction: column; gap: 2px;"><span style="font: 600 15px/20px {SANS}; color: var(--fg-default);">{H("f.name")}</span>'
             f'<span style="font: 400 12px/16px {MONO}; color: var(--fg-muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">{H("f.path")}</span><span style="{FN}">{H("f.meta")}</span></span></div>'
             f'<div style="display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px;">{btn("secondary", "Превью", "eye", "f.preview", "padding: 0 8px;")}{btn("secondary", "В тред", None, "f.attach", "padding: 0 8px;")}{btn("secondary", "Скачать", "download", "f.download", "padding: 0 8px;")}</div></div>')
    files = f'<div style="display: flex; flex-direction: column; gap: 10px; flex-shrink: 0;">{SFOR("m.files", "f", fcard, 2)}</div>'
    items = (SIF('m.isSys', sysn) + SIF('m.isOwner', owner) + SIF('m.isBot', botm) + SIF('m.isLink', link) + SIF('m.isPlan', plan) + SIF('m.isFiles', files))
    msgs = f'<div class="scr" style="flex-grow: 1; min-height: 0; overflow-y: auto; padding: 16px; display: flex; flex-direction: column-reverse; gap: 12px; scrollbar-width: none;">{SFOR("thread.rev", "m", items, 4)}</div>'
    running = f'<div style="padding: 10px 12px 26px; background: var(--bg-surface); border-top: 1px solid var(--border-default); display: flex; align-items: center; gap: 8px; flex-shrink: 0;"><span style="flex-grow: 1; font: 500 15px {SANS}; color: var(--fg-muted);">{H("thread.runLabel")}</span>{btn("danger", "Стоп", "stop", "stop")}</div>'
    pick = f'<button type="button" onClick="{H("cycleModel")}" aria-label="Сменить модель" style="min-height: 32px; padding: 0 10px; border-radius: 8px; border: none; background: var(--claude-bg); color: var(--claude-fg); font: 600 12px {SANS}; display: inline-flex; align-items: center; gap: 4px;">{H("cur.modelLabel")}{ic("down", 12, 2.6)}</button>'
    idle = (f'<div style="padding: 10px 12px 26px; background: var(--bg-surface); border-top: 1px solid var(--border-default); display: flex; flex-direction: column; gap: 8px; flex-shrink: 0;">'
            f'<div style="display: flex; align-items: center; gap: 10px;">{pick}<label style="display: flex; align-items: center; gap: 6px; {FN}"><input type="checkbox" checked="{H("dry")}" onChange="{H("toggleDry")}" style="width: 18px; height: 18px; margin: 0;">Пробный прогон</label></div>'
            f'<div style="display: flex; align-items: center; gap: 8px;"><label for="msg" style="position: absolute; width: 1px; height: 1px; overflow: hidden;">Сообщение</label>'
            f'<input id="msg" type="text" value="{H("cur.draft")}" onInput="{H("onDraft")}" onChange="{H("onDraft")}" onKeyDown="{H("onDraftKey")}" placeholder="Сообщение" style="flex-grow: 1; min-width: 0; height: 44px; box-sizing: border-box; padding: 0 16px; border-radius: 22px; border: 1px solid var(--border-control); background: var(--bg-sunken); color: var(--fg-default); font: 400 15px {SANS};">'
            f'{iconbtn("mic", "Голосовой ввод", "ghost", "dictate", True)}{iconbtn("send", "Отправить", "primary", "send", True)}</div></div>')
    return SIF('isThread', f'<div style="{COL}">{header}{msgs}{SIF("thread.running", running)}{SIF("thread.idle", idle, True)}</div>')


def screen_approvals():
    dl = (f'<dt style="{FN} color: var(--attention-text);">{H("r.k")}</dt><dd style="margin: 0; font: 400 15px/20px {SANS}; color: var(--fg-default);">{H("r.v")}</dd>')
    card = (f'<section style="{CARD} padding: 14px; display: flex; flex-direction: column; gap: 12px;">'
            f'<div style="display: flex; align-items: center; gap: 10px;">{avatar_dyn("a", 36)}<div style="flex-grow: 1; display: flex; flex-direction: column;"><span style="font: 600 17px/22px {SANS}; color: var(--fg-default);">{H("a.botName")}</span><span style="{FN}">{H("a.expires")}</span></div></div>'
            f'<div style="padding: 14px; border-radius: 16px; background: var(--attention-bg); border: 1px solid var(--attention-border); display: flex; flex-direction: column; gap: 12px;">'
            f'<div style="display: flex; align-items: center; gap: 6px; color: var(--attention-text); font: 600 12px/16px {SANS}; letter-spacing: 0.02em;">{ic("risk", 16, 2.2)}{H("a.category")}</div>'
            f'<div style="font: 500 17px/22px {SANS}; color: var(--fg-default); overflow-wrap: anywhere;">{H("a.title")}</div>'
            f'<dl style="margin: 0; display: grid; grid-template-columns: auto 1fr; gap: 6px 14px; align-items: baseline;">{SFOR("a.rows", "r", dl, 3)}</dl>'
            f'<details><summary style="min-height: 32px; display: flex; align-items: center; {FN} color: var(--attention-text); cursor: pointer;">Аргументы действия</summary><pre style="margin: 0; padding: 8px 10px; border-radius: 10px; background: var(--bg-surface); font: 400 12px/16px {MONO}; color: var(--fg-default); white-space: pre-wrap; overflow-wrap: anywhere;">{H("a.args")}</pre></details></div>'
            f'<label style="display: flex; gap: 10px; align-items: flex-start; font: 400 15px/20px {SANS}; color: var(--fg-default);"><input type="checkbox" checked="{H("a.always")}" onChange="{H("a.toggleAlways")}" style="width: 20px; height: 20px; margin: 0; flex-shrink: 0;"><span>{H("a.alwaysLabel")}<br><span style="{FN}">Правило можно отключить в настройках бота</span></span></label>'
            f'<div style="display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px;">{btn("secondary", "Отклонить", None, "a.reject", "padding: 0 8px;")}{btn("secondary", "Изменить", None, "a.edit", "padding: 0 8px;")}{btn("approve", "Разрешить", None, "a.approve", "padding: 0 8px;")}</div></section>')
    empty = SIF('ap.empty', f'<div style="{CARD} padding: 24px 16px; display: flex; flex-direction: column; align-items: center; gap: 8px; text-align: center;"><span style="color: var(--success-fg); display: flex;">{ic("check", 28, 2.4)}</span><span style="font: 600 17px/22px {SANS};">Всё разобрано</span><span style="{FN}">Решений нет. Боты работают без остановок.</span></div>')
    body = f'<div class="scr" style="{SCROLL} padding-top: 4px;">{empty}{SFOR("ap.items", "a", card, 2)}</div>'
    return SIF('isApprovals', f'<div style="{COL}">{header_root("Решения")}{body}</div>')


def screen_handoff():
    dots = ''.join(f'<span style="width: 38px; height: 46px; border-radius: 10px; border: 2px solid var(--{"focus" if i == 0 else "border-control"}); background: var(--bg-surface);"></span>' for i in range(6))
    screen = (f'<div style="margin: 0 16px; border-radius: 16px; overflow: hidden; border: 1px solid var(--border-default); background: var(--bg-surface); flex-shrink: 0;">'
              f'<div style="height: 30px; display: flex; align-items: center; gap: 6px; padding: 0 10px; background: var(--bg-sunken); {FN} font-size: 12px;"><span style="width: 8px; height: 8px; border-radius: 4px; background: var(--border-control);"></span><span style="width: 8px; height: 8px; border-radius: 4px; background: var(--border-control);"></span><span style="margin-left: 8px; font-family: {MONO};">accounts.example.eu/verify</span></div>'
              f'<div style="height: 300px; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 14px; padding: 20px; background: var(--bg-canvas);"><div style="font: 600 17px {SANS}; color: var(--fg-default);">Введите код из SMS</div><div style="display: flex; gap: 8px;">{dots}</div><div style="{FN}">[Экран бота: живой поток noVNC]</div></div></div>')
    banner = f'<div style="padding: 12px 16px 0; flex-shrink: 0;"><div style="display: flex; align-items: center; gap: 10px; padding: 12px 14px; border-radius: 14px; background: var(--attention-bg); border: 1px solid var(--attention-border); color: var(--attention-text); font: 600 15px/20px {SANS};">{ic("hand", 20)}<span>Бот ждёт тебя: код 2FA. Управление у тебя.</span></div></div>'
    log = f'<div style="padding: 12px 16px; display: flex; flex-direction: column; gap: 6px; font: 400 12px/16px {MONO}; color: var(--fg-muted); flex-shrink: 0;"><span>09:52:10 browser.open accounts.example.eu</span><span>09:52:14 форма входа заполнена</span><span style="color: var(--attention-fg);">09:52:15 ждёт 2FA, управление передано владельцу</span></div>'
    foot = f'<div style="padding: 10px 16px 28px; display: grid; grid-template-columns: 1fr auto; gap: 8px; background: var(--bg-surface); border-top: 1px solid var(--border-default); flex-shrink: 0;">{btn("primary", "Вернуть боту", "play", "hand.giveBack")}{btn("danger", "Стоп", "stop", "hand.stop")}</div>'
    return SIF('isHandoff', f'<div style="{COL}">{header_back(H("hand.title"), H("cur.screenSub"), avatar_dyn("cur", 36))}{banner}<div style="height: 12px; flex-shrink: 0;"></div>{screen}{log}<div style="flex-grow: 1;"></div>{foot}</div>')


def routine_card(lst, icon, n):
    card = (f'<div style="{CARD} padding: 12px 14px; display: flex; flex-direction: column; gap: 10px;">'
            f'<button type="button" onClick="{H("r.open")}" style="{RESET_BTN} width: 100%; display: flex; align-items: center; gap: 12px;">'
            f'<span style="width: 40px; height: 40px; flex-shrink: 0; border-radius: 12px; background: var(--bg-sunken); color: var(--fg-default); display: flex; align-items: center; justify-content: center;">{ic(icon, 20)}</span>'
            f'<span style="flex-grow: 1; min-width: 0; display: flex; flex-direction: column; gap: 2px;"><span style="font: 600 15px/20px {SANS};">{H("r.title")}</span><span style="{FN} white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">{H("r.sub")}</span><span style="{FN}">{H("r.last")}</span></span>{ic("chev", 16, 2.2)}</button>'
            f'<div style="display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px;">{btn("secondary", "Запустить сейчас", "play", "r.run", "padding: 0 6px; gap: 6px; font-size: 14px; white-space: nowrap;")}'
            f'{SIF("r.enabled", btn("secondary", "Пауза", None, "r.toggle", "padding: 0 8px;"), True)}{SIF("r.paused", btn("primary", "Включить", None, "r.toggle", "padding: 0 8px;"))}</div></div>')
    return SFOR(lst, 'r', card, n)


def screen_routines():
    body = (f'<div class="scr" style="{SCROLL} padding-top: 4px; gap: 10px;">{section("По расписанию")}{routine_card("rt.sched", "clock", 2)}'
            f'{section("По событию")}{routine_card("rt.event", "bolt", 2)}{section("Процедуры")}{routine_card("rt.proc", "list", 1)}</div>')
    return SIF('isRoutines', f'<div style="{COL}">{header_root("Рутины", btn("secondary", "Новая", "plus", "addRoutine"))}{body}</div>')


def screen_procedure():
    def field(label, val, on, id_):
        return f'<div style="display: flex; flex-direction: column; gap: 6px;"><label for="{id_}" style="{FN}">{label}</label><input id="{id_}" type="text" value="{H(val)}" onInput="{H(on)}" onChange="{H(on)}" style="height: 44px; box-sizing: border-box; padding: 0 14px; border-radius: 12px; border: 1px solid var(--border-control); background: var(--bg-sunken); color: var(--fg-default); font: 400 15px {MONO};"></div>'
    params = f'<div style="{CARD} padding: 14px; display: flex; flex-direction: column; gap: 12px;"><span style="font: 600 17px {SANS};">Параметры</span>{field("Домен", "proc.domain", "proc.onDomain", "p1")}{field("Сервер", "proc.server", "proc.onServer", "p2")}</div>'
    appr = f'<span style="display: inline-flex; align-items: center; gap: 4px; padding: 2px 7px; border-radius: 6px; background: var(--attention-bg); color: var(--attention-text); font: 600 12px {SANS};">{ic("shield", 12, 2.4)}подтверждение</span>'
    step = (f'<li style="display: flex; gap: 12px; align-items: flex-start;"><span style="width: 24px; height: 24px; flex-shrink: 0; border-radius: 12px; background: {H("s.bg")}; color: {H("s.fg")}; display: flex; align-items: center; justify-content: center; font: 600 12px {MONO};">'
            f'{SIF("s.isDone", ic("check", 14, 2.8))}{SIF("s.notDone", H("s.n"), True)}</span>'
            f'<span style="display: flex; flex-direction: column; gap: 4px; font: 400 15px/20px {SANS}; color: var(--fg-default);">{H("s.text")}{SIF("s.hasApproval", appr)}<span style="{FN} font-size: 12px;">{H("s.stateText")}</span></span></li>')
    steps = f'<div style="{CARD} padding: 14px; display: flex; flex-direction: column; gap: 12px;"><span style="font: 600 17px {SANS};">Шаги</span><ol style="list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 12px;">{SFOR("proc.steps", "s", step, 4)}</ol></div>'
    result = SIF('proc.hasResult', f'<div style="padding: 12px 14px; border-radius: 14px; background: var(--bg-surface); border: 1px solid var(--success-fg); display: flex; align-items: center; gap: 10px; color: var(--success-fg); font: 600 15px/20px {SANS}; flex-shrink: 0;">{ic("check", 18, 2.6)}<span>{H("proc.result")}</span></div>')
    info = f'<div style="{FN} padding: 0 4px; flex-shrink: 0;">{H("proc.info")}</div>'
    sched = (SIF('proc.sched', f'<button type="button" aria-pressed="true" onClick="{H("proc.toggleSched")}" style="min-height: 44px; padding: 0 16px; border-radius: 12px; background: var(--bg-sunken); color: var(--fg-default); border: 1px solid var(--focus); font: 600 15px {SANS}; display: inline-flex; align-items: center; justify-content: center; gap: 8px;">{ic("check", 16, 2.6)}По расписанию</button>')
             + SIF('proc.notSched', btn('secondary', 'По расписанию', 'clock', 'proc.toggleSched'), True))
    run = f'<button type="button" onClick="{H("proc.run")}" disabled="{H("proc.running")}" style="min-height: 44px; padding: 0 16px; border-radius: 12px; background: var(--bg-emphasis); color: var(--fg-on-emphasis); border: 1px solid transparent; font: 500 15px {SANS}; display: inline-flex; align-items: center; justify-content: center; gap: 8px; opacity: {H("proc.runOpacity")};">{ic("play", 16, 2.4)}{H("proc.runLabel")}</button>'
    foot = f'<div style="padding: 10px 16px 28px; display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; background: var(--bg-surface); border-top: 1px solid var(--border-default); flex-shrink: 0;">{sched}{run}</div>'
    return SIF('isProcedure', f'<div style="{COL}">{header_back("Перевыпуск сертификата", "Процедура · SRE · создана из задачи 21 сен", avatar_dyn("procBot", 36))}<div class="scr" style="{SCROLL}">{params}{steps}{result}{info}</div>{foot}</div>')


def screen_incident():
    tl = f'<li style="display: grid; grid-template-columns: 52px 1fr; gap: 10px; font: 400 14px/20px {SANS}; color: var(--fg-default);"><span style="font: 400 12px/20px {MONO}; color: {H("t.color")};">{H("t.time")}</span><span>{H("t.text")}</span></li>'

    def hyp(title, ev, verdict, c):
        return f'<div style="{CARD} padding: 12px 14px; display: flex; flex-direction: column; gap: 6px;"><div style="display: flex; justify-content: space-between; gap: 8px; align-items: center;"><span style="font: 600 15px/20px {SANS};">{title}</span><span style="font: 600 12px {SANS}; color: var(--{c}); white-space: nowrap;">{verdict}</span></div><span style="font: 400 12px/16px {MONO}; color: var(--fg-muted);">{ev}</span></div>'
    chips = (f'<div style="display: flex; gap: 8px; flex-wrap: wrap; flex-shrink: 0;"><span style="padding: 4px 10px; border-radius: 6px; background: {H("inc.chipBg")}; color: {H("inc.chipFg")}; font: 600 12px {SANS};">{H("inc.chipText")}</span>'
             f'<span style="padding: 4px 10px; border-radius: 6px; background: var(--bg-sunken); color: var(--fg-muted); font: 600 12px {SANS};">2 мин сбора · 6 источников</span></div>')
    timeline = f'<ol style="list-style: none; margin: 0; padding: 12px 14px; {CARD} display: flex; flex-direction: column; gap: 6px;">{SFOR("inc.timeline", "t", tl, 4)}</ol>'
    pending = SIF('inc.pending', f'<div style="padding: 12px 14px; border-radius: 16px; background: var(--attention-bg); border: 1px solid var(--attention-border); display: flex; flex-direction: column; gap: 10px; flex-shrink: 0;"><span style="font: 500 15px/20px {SANS}; color: var(--fg-default);">Предлагаю: поднять лимит до 2 ГБ и перезапустить webapp</span><div style="display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px;">{btn("secondary", "Отклонить", None, "inc.reject")}{btn("approve", "Разрешить", None, "inc.approve")}</div></div>', True)
    decided = SIF('inc.decided', f'<div style="padding: 12px 14px; border-radius: 16px; background: {H("inc.decisionBg")}; border: 1px solid var(--border-default); display: flex; align-items: center; gap: 10px; color: {H("inc.decisionFg")}; font: 600 15px/20px {SANS}; flex-shrink: 0;">{ic("shield", 18, 2.4)}<span>{H("inc.decisionText")}</span></div>')
    body = (f'<div class="scr" style="{SCROLL} gap: 10px;">{chips}{timeline}{section("Гипотезы")}'
            f'{hyp("Не хватает памяти после деплоя", "docker events: oom-kill ×4 · dmesg: Killed process", "вероятно", "attention-fg")}{hyp("Кончился диск", "df -h /: занято 41%", "опровергнута", "fg-muted")}{pending}{decided}</div>')
    return SIF('isIncident', f'<div style="{COL}">{header_back("Инцидент · 502 webapp", H("inc.sub"), avatar_dyn("procBot", 36))}{body}</div>')


def screen_usage():
    subs = f'<div style="{CARD} padding: 14px 16px; display: flex; flex-direction: column; gap: 10px;"><div style="display: flex; justify-content: space-between; {FN}"><span>Подписки, неделя</span><span>сброс пн 03:00</span></div>{meter_loop("usage.subs")}</div>'
    guard_on = SIF('usage.guardOn', f'<div style="padding: 14px; border-radius: 16px; background: var(--danger-bg); color: var(--danger-fg); display: flex; flex-direction: column; gap: 10px; flex-shrink: 0;"><div style="display: flex; gap: 8px; align-items: flex-start; font: 600 15px/20px {SANS};">{ic("stop", 18, 2.4)}<span>Кодер остановлен предохранителем: 3 раза одна ошибка</span></div><code style="font: 400 12px/16px {MONO}; color: var(--fg-default);">npm ERR! ERESOLVE unable to resolve dependency tree</code><div style="display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px;">{btn("secondary", "Открыть тред", None, "usage.openThread")}{btn("secondary", "Другая модель", None, "usage.swapModel")}</div></div>', True)
    guard_off = SIF('usage.guardOff', f'<div style="{CARD} padding: 14px; display: flex; align-items: center; gap: 10px; color: var(--success-fg); font: 600 15px/20px {SANS};">{ic("check", 18, 2.6)}<span>{H("usage.guardText")}</span></div>')
    budgets = f'<div style="{CARD} padding: 14px 16px; display: flex; flex-direction: column; gap: 10px;"><div style="{FN}">Дневной бюджет ботов</div>{meter_loop("usage.budgets")}</div>'
    return SIF('isUsage', f'<div style="{COL}">{header_root("Расход")}<div class="scr" style="{SCROLL} padding-top: 4px;">{subs}{guard_on}{guard_off}{budgets}</div></div>')


def screen_memory():
    search = f'<div style="display: flex; align-items: center; gap: 8px; height: 44px; padding: 0 14px; border-radius: 12px; background: var(--bg-sunken); color: var(--fg-muted); flex-shrink: 0;">{ic("search", 18)}<label for="ms" style="position: absolute; width: 1px; height: 1px; overflow: hidden;">Поиск по памяти</label><input id="ms" type="text" value="{H("mem.q")}" onInput="{H("mem.onQ")}" onChange="{H("mem.onQ")}" placeholder="Поиск по памяти" style="flex-grow: 1; min-width: 0; border: none; background: transparent; outline: none; font: 400 15px {SANS}; color: var(--fg-default);"></div>'
    prop = SIF('mem.hasProposal', f'{section("Бот предлагает запомнить")}<div style="padding: 12px 14px; {CARD} display: flex; flex-direction: column; gap: 10px;"><span style="font: 400 15px/20px {SANS};">{H("mem.proposal.text")}</span><span style="{FN} font-size: 12px;">{H("mem.proposal.src")}</span><div style="display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px;">{btn("secondary", "Не запоминать", None, "mem.dismiss", "padding: 0 8px;")}{btn("primary", "Запомнить", None, "mem.remember", "padding: 0 8px;")}</div></div>')
    fact = (f'<div style="display: flex; gap: 10px; align-items: center; padding: 10px 12px 10px 14px; {CARD}"><span style="flex-grow: 1; min-width: 0; display: flex; flex-direction: column; gap: 2px;"><span style="font: 400 15px/20px {SANS}; color: var(--fg-default); overflow-wrap: anywhere;">{H("f.text")}</span><span style="{FN} font-size: 12px;">{H("f.src")}</span></span>'
            f'<button type="button" aria-label="Удалить факт" onClick="{H("f.remove")}" style="width: 44px; height: 44px; flex-shrink: 0; padding: 0; border: none; border-radius: 12px; background: transparent; color: var(--fg-muted); display: flex; align-items: center; justify-content: center;">{ic("x", 18, 2.2)}</button></div>')
    nores = SIF('mem.noFacts', f'<div style="{CARD} padding: 16px; {FN} text-align: center;">Ничего не найдено</div>')
    body = f'<div class="scr" style="{SCROLL} gap: 10px;">{search}{prop}{section("Факты")}{nores}{SFOR("mem.facts", "f", fact, 4)}</div>'
    return SIF('isMemory', f'<div style="{COL}">{header_back("Память", H("mem.sub"))}{body}</div>')


def screen_settings():
    tiles = ''
    for k, n in [('mac', 'Мак'), ('scout', 'Скаут'), ('sre', 'SRE'), ('coder', 'Кодер'), ('archive', 'Архив'), ('owl', 'Сова'), ('spark', 'Искра'), ('robot', 'Робот')]:
        p = f'personas.{k}'
        tiles += f'<button type="button" aria-label="Персонаж: {n}" aria-pressed="{H(p + ".pressed")}" onClick="{H(p + ".pick")}" style="width: 40px; height: 40px; padding: 0; border-radius: 12px; border: 2px solid {H(p + ".border")}; background: transparent; display: flex; align-items: center; justify-content: center;">{avatars.avatar_svg(k, 32)}</button>'
    persona = (f'<div style="{CARD} padding: 14px; display: flex; flex-direction: column; gap: 12px;"><div style="display: flex; align-items: center; gap: 14px;">{avatar_dyn("cur", 64)}'
               f'<div style="flex-grow: 1; min-width: 0; display: flex; flex-direction: column; gap: 2px;"><span style="font: 600 17px/22px {SANS};">{H("cur.name")}</span><span style="{FN}">{H("cur.desc")}</span></div>{btn("secondary", "Другой", None, "nextPersona", "padding: 0 12px;")}</div>'
               f'<div style="display: flex; justify-content: space-between;">{tiles}</div></div>')
    model = group('Модель', seg_loop('set.modelOpts', 3))
    toggle = (f'<div style="display: flex; align-items: center; gap: 12px; min-height: 44px;"><label for="{H("t.id")}" style="flex-grow: 1; display: flex; flex-direction: column; gap: 2px;"><span style="font: 400 15px/20px {SANS}; color: var(--fg-default);">{H("t.label")}</span><span style="{FN} font-size: 12px;">{H("t.sub")}</span></label>'
              f'<input id="{H("t.id")}" type="checkbox" role="switch" checked="{H("t.on")}" onChange="{H("t.toggle")}" style="width: 44px; height: 26px; margin: 0; accent-color: var(--bg-emphasis);"></div>')
    where = group('Где работает', seg_loop('set.whereOpts', 2) + SFOR('set.toggles', 't', toggle, 3))
    chip_st = f'min-height: 36px; padding: 0 10px 0 12px; border-radius: 18px; font: 500 13px {SANS}; display: inline-flex; align-items: center; gap: 6px;'
    chip_auto = f'<button type="button" aria-label="Удалить правило: {H("c.label")}" onClick="{H("c.remove")}" style="{chip_st} border: 1px solid var(--border-control); background: var(--bg-sunken); color: var(--fg-default);">{H("c.label")}{ic("x", 14, 2.4)}</button>'
    chip_ask = f'<button type="button" aria-label="Удалить правило: {H("c.label")}" onClick="{H("c.remove")}" style="{chip_st} border: 1px solid var(--attention-border); background: var(--attention-bg); color: var(--attention-text);">{H("c.label")}{ic("x", 14, 2.4)}</button>'
    no_rules = f'<span style="{FN}">Нет правил</span>'
    rules = group('Правила', f'<span style="{FN}">Без спроса</span><div style="display: flex; flex-wrap: wrap; gap: 8px;">{SFOR("set.autoChips", "c", chip_auto, 4)}{SIF("set.noAuto", no_rules)}</div>'
                  f'<span style="{FN} color: var(--attention-text);">Всегда спрашивать</span><div style="display: flex; flex-wrap: wrap; gap: 8px;">{SFOR("set.askChips", "c", chip_ask, 4)}{SIF("set.noAsk", no_rules)}</div>'
                  f'<span style="{FN} font-size: 12px;">Нажми на правило, чтобы удалить его.</span>')
    body = f'<div class="scr" style="{SCROLL} gap: 10px;">{persona}{model}{where}{rules}</div>'
    return SIF('isSettings', f'<div style="{COL}">{header_back(H("cur.name"), "Настройки бота", avatar_dyn("cur", 36))}{body}</div>')


def screen_newbot():
    hint = f'<span style="font: 400 13px/18px {SANS}; color: {H("nb.hintColor")};">Опиши задачу хотя бы в нескольких словах</span>'
    brief = (f'<div style="{CARD} padding: 14px; display: flex; flex-direction: column; gap: 10px;"><label for="nbt" style="font: 600 15px {SANS}; color: var(--fg-default);">Что должен делать бот</label>'
             f'<textarea id="nbt" rows="5" value="{H("nb.brief")}" onInput="{H("nb.onBrief")}" onChange="{H("nb.onBrief")}" placeholder="Например: каждое утро собирай новости про Kubernetes и присылай короткую сводку" style="box-sizing: border-box; width: 100%; padding: 12px 14px; border-radius: 12px; border: 1px solid var(--border-control); background: var(--bg-sunken); color: var(--fg-default); font: 400 15px/20px {SANS}; resize: none;"></textarea>'
             f'{SIF("nb.showHint", hint)}'
             f'<div style="display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px;">{btn("secondary", "Пример", None, "nb.example")}'
             f'<button type="button" onClick="{H("nb.build")}" disabled="{H("nb.buildDisabled")}" style="min-height: 44px; padding: 0 16px; border-radius: 12px; background: var(--bg-emphasis); color: var(--fg-on-emphasis); border: 1px solid transparent; font: 500 15px {SANS}; opacity: {H("nb.buildOpacity")};">{H("nb.buildLabel")}</button></div></div>')
    draft_name = f'<div style="display: flex; flex-direction: column; gap: 6px;"><label for="nbn" style="{FN}">Имя</label><input id="nbn" type="text" value="{H("nb.d.name")}" onInput="{H("nb.onName")}" onChange="{H("nb.onName")}" style="height: 44px; box-sizing: border-box; padding: 0 14px; border-radius: 12px; border: 1px solid var(--border-control); background: var(--bg-sunken); color: var(--fg-default); font: 400 15px {SANS};"></div>'
    sch_text = lambda color, title: f'<div style="flex-grow: 1; min-width: 0; display: flex; flex-direction: column; gap: 2px;"><span style="font: 500 15px/20px {SANS}; color: var(--{color});">{title}</span><span style="font: 400 12px/16px {MONO}; color: var(--fg-muted);">{H("nb.sch.cron")}</span></div>'
    sch_on = f'<div style="display: flex; align-items: center; gap: 10px;">{sch_text("fg-default", H("nb.sch.human"))}{btn("secondary", "Выключить", None, "nb.toggleSchedule", "padding: 0 12px;")}</div>'
    sch_off = f'<div style="display: flex; align-items: center; gap: 10px;">{sch_text("fg-muted", "Выключено: рутина не будет создана")}{btn("primary", "Включить", None, "nb.toggleSchedule", "padding: 0 12px;")}</div>'
    sch = SIF('nb.hasSchedule', f'<div style="padding: 12px 14px; border-radius: 14px; background: var(--bg-sunken); display: flex; flex-direction: column; gap: 8px;"><span style="display: flex; align-items: center; gap: 6px; font: 600 13px/18px {SANS}; color: var(--fg-muted);">{ic("clock", 14, 2.2)}Расписание</span>{SIF("nb.sch.on", sch_on)}{SIF("nb.sch.off", sch_off)}</div>')
    draft = SIF('nb.isDraft', (f'<div style="{CARD} padding: 14px; display: flex; flex-direction: column; gap: 12px;"><div style="display: flex; align-items: center; gap: 14px;">{avatar_dyn("nb.d", 64)}'
                               f'<div style="flex-grow: 1; min-width: 0; display: flex; flex-direction: column; gap: 2px;"><span style="font: 600 17px/22px {SANS};">Черновик бота</span><span style="{FN}">{H("nb.d.desc")}</span></div></div>{draft_name}'
                               f'<span style="{FN}">Модель</span>{seg_loop("nb.modelOpts", 3)}<span style="{FN}">Где работает</span>{seg_loop("nb.whereOpts", 2)}{sch}'
                               f'<span style="{FN} font-size: 12px;">Без спроса: чтение, поиск, скриншот. Всегда спрашивать: удаление, отправка, оплата, вход.</span>'
                               f'<div style="display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px;">{btn("secondary", "Собрать заново", None, "nb.rebuild", "padding: 0 8px;")}{btn("primary", "Создать", "check", "nb.create", "padding: 0 8px;")}</div></div>'))
    body = f'<div class="scr" style="{SCROLL} gap: 10px;">{brief}{draft}</div>'
    return SIF('isNewBot', f'<div style="{COL}">{header_back("Новый бот", "Опиши задачу, остальное соберу сам")}{body}</div>')


def tabbar():
    items = ''
    for key, icn, t in [('bots', 'bot', 'Боты'), ('routines', 'clock', 'Рутины'), ('approvals', 'shield', 'Решения'), ('usage', 'bars', 'Расход')]:
        dot = SIF('tabDot', '<span aria-label="есть ожидающие" style="position: absolute; top: 4px; right: 30%; width: 8px; height: 8px; border-radius: 4px; background: var(--attention-fg);"></span>') if key == 'approvals' else ''
        on, cur, col = H(f'tab.{key}.open'), H(f'tab.{key}.current'), H(f'tab.{key}.color')
        items += f'<button type="button" onClick="{on}" aria-current="{cur}" style="position: relative; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 3px; min-height: 48px; padding: 0; border: none; background: transparent; color: {col}; font: 600 11px/14px {SANS};">{ic(icn, 22)}{t}{dot}</button>'
    nav = f'<nav aria-label="Разделы" style="display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); padding: 6px 8px 22px; background: var(--bg-glass); border-top: 1px solid var(--border-default); backdrop-filter: blur(20px); flex-shrink: 0;">{items}</nav>'
    return SIF('showTabs', nav, True)


def toast():
    openb = SIF('toast.hasOpen', f'<button type="button" onClick="{H("toast.open")}" style="min-height: 36px; padding: 0 12px; border-radius: 10px; border: none; background: var(--bg-surface); color: var(--fg-default); font: 600 14px {SANS};">{H("toast.label")}</button>')
    return SIF('hasToast', f'<div role="status" style="position: absolute; left: 16px; right: 16px; bottom: 96px; z-index: 5; display: flex; align-items: center; gap: 10px; padding: 10px 12px 10px 14px; border-radius: 14px; background: var(--bg-emphasis); color: var(--fg-on-emphasis); font: 500 14px/20px {SANS}; box-shadow: 0 8px 24px rgba(22, 24, 26, 0.18);"><span style="flex-grow: 1;">{H("toast.text")}</span>{openb}</div>')


# ---------------------------------------------------------------- логика (plain JS, DCLogic)

SCRIPT = r"""
class Component extends DCLogic {
  constructor(props) {
    super(props);
    this.timers = [];
    this.runTimers = {};
    this.uid = 1000;
    this.K = this.makeConsts();
    this.state = this.initState();
    this.S = this.state;
  }

  componentWillUnmount() {
    this.timers.forEach((t) => clearTimeout(t));
    this.timers = [];
    this.runTimers = {};
  }

  // S: синхронная копия state. Таймеры и обработчики читают её, чтобы подряд идущие
  // обновления не затирали друг друга; this.state нужен только для рендера.
  commit(patch) {
    this.S = Object.assign({}, this.S, patch);
    this.setState(patch);
  }
  // key (runId) группирует таймеры прогона, чтобы stop() и новый прогон снимали их через clearTimeout
  after(ms, fn, key) {
    const id = setTimeout(() => { this.forget(id, key); fn(); }, ms);
    this.timers.push(id);
    if (key) (this.runTimers[key] = this.runTimers[key] || []).push(id);
  }
  forget(id, key) {
    this.timers = this.timers.filter((t) => t !== id);
    if (key && this.runTimers[key]) this.runTimers[key] = this.runTimers[key].filter((t) => t !== id);
  }
  clearRun(key) {
    (this.runTimers[key] || []).forEach((t) => clearTimeout(t));
    this.timers = this.timers.filter((t) => (this.runTimers[key] || []).indexOf(t) < 0);
    delete this.runTimers[key];
  }
  nextId(p) { this.uid += 1; return p + this.uid; }
  hhmm() {
    const d = new Date();
    return String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
  }
  short(t) { return t.length > 40 ? t.slice(0, 40) + '…' : t; }
  fmtSecs(s) { return s < 60 ? s + ' с' : Math.floor(s / 60) + ' мин ' + String(s % 60).padStart(2, '0') + ' с'; }
  plural(n, f) {
    const a = Math.abs(n) % 100, b = a % 10;
    if (a > 10 && a < 20) return f[2];
    if (b > 1 && b < 5) return f[1];
    return b === 1 ? f[0] : f[2];
  }

  makeConsts() {
    return {
      models: {
        haiku: { label: 'Haiku 4.5', short: 'Haiku', tokens: 9, secs: 6, quota: 1 },
        sonnet: { label: 'Sonnet 5.5', short: 'Sonnet', tokens: 24, secs: 11, quota: 2 },
        opus: { label: 'Opus 5.5', short: 'Opus', tokens: 58, secs: 24, quota: 4 },
      },
      modelOrder: ['haiku', 'sonnet', 'opus'],
      personas: {
        scout: { name: 'Скаут', desc: 'Ищет вакансии и страницы, следит за изменениями' },
        mac: { name: 'Мак', desc: 'Ищет файлы, открывает приложения, делает скриншоты' },
        sre: { name: 'SRE', desc: 'Диагностика серверов, логи, инциденты' },
        coder: { name: 'Кодер', desc: 'Пишет и проверяет код, гоняет тесты' },
        archive: { name: 'Архив', desc: 'Раскладывает файлы и ведёт архив' },
        owl: { name: 'Сова', desc: 'Исследует тему и собирает источники' },
        spark: { name: 'Искра', desc: 'Придумывает идеи и варианты' },
        robot: { name: 'Робот', desc: 'Универсальный помощник' },
      },
      personaOrder: ['mac', 'scout', 'sre', 'coder', 'archive', 'owl', 'spark', 'robot'],
      statuses: {
        ok: ['Готово', 'success-fg'],
        work: ['Работает', 'success-fg'],
        wait: ['Ждёт тебя', 'attention-fg'],
        stop: ['Стоп', 'danger-fg'],
        idle: ['Ждёт события', 'neutral-fg'],
        halt: ['Остановлен', 'neutral-fg'],
        off: ['Mac не в сети', 'neutral-fg'],
      },
      scripts: {
        scout: { steps: ['Открыть job-борды', 'Отфильтровать по стеку и зарплате', 'Сверить с резюме EN'], tools: 'Chromium', reply: 'Просмотрел 12 страниц, оставил 5 подходящих вакансий. Лучшая: Senior SRE, Belgrade/remote.' },
        mac: { steps: ['Spotlight: поиск по имени', 'Проверить даты изменения', 'Собрать превью'], tools: 'Spotlight, QuickLook', reply: 'Нашёл подходящие файлы, самый свежий изменён вчера.' },
        sre: { steps: ['docker ps и логи контейнеров', 'dmesg и df -h', 'Свести гипотезы'], tools: 'docker, dmesg', reply: 'Серверы в порядке: память 61%, диск 41%, контейнеры живы. Ничего подозрительного.' },
        coder: { steps: ['Прочитать репозиторий', 'Внести правку', 'Прогнать тесты'], tools: 'git, pytest', reply: 'Правка готова, тесты зелёные: 133 passed.' },
        archive: { steps: ['Просмотреть inbox', 'Определить тип файлов', 'Разложить по папкам'], tools: 'Finder', reply: 'Разложил 4 файла по папкам, дубликатов нет.' },
        owl: { steps: ['Найти источники', 'Выписать факты', 'Свести выводы'], tools: 'WebFetch', reply: 'Собрал 6 источников и короткую сводку.' },
        spark: { steps: ['Набросать варианты', 'Отсеять слабые', 'Оформить лучшие'], tools: 'заметки', reply: 'Оставил 3 идеи из 9, лучшая сверху.' },
        robot: { steps: ['Разобрать задачу', 'Выполнить шаги', 'Проверить результат'], tools: 'shell', reply: 'Готово, результат сохранён в треде.' },
      },
      dictation: {
        scout: 'Найди свежие вакансии SRE в Европе', mac: 'Найди на маке презентацию за май', sre: 'Проверь диск и память на сервере',
        coder: 'Прогони тесты в core', archive: 'Разложи новые сканы по папкам', owl: 'Собери источники по теме', spark: 'Предложи три идеи для поста', robot: 'Сделай сводку за день',
      },
    };
  }

  newRules() {
    const a = (id, label, auto) => ({ id: id + this.nextId(''), label, auto });
    return [a('r', 'Чтение файлов', true), a('r', 'Spotlight', true), a('r', 'Открыть приложение', true), a('r', 'Скриншот', true),
      a('r', 'Удаление', false), a('r', 'Отправка', false), a('r', 'Оплата', false), a('r', 'Вход', false)];
  }

  initState() {
    const tg = () => ({ full: true, confirm: true, guard: true });
    const bots = [
      { id: 'scout', name: 'Скаут', kind: 'scout', model: 'sonnet', where: 'server', status: 'wait', last: 'Нашёл 7 вакансий SRE, жду решения по отклику' },
      { id: 'mac', name: 'Мак', kind: 'mac', model: 'sonnet', where: 'mac', status: 'ok', last: 'Нашёл 3 файла: договор аренды 2025' },
      { id: 'sre', name: 'SRE', kind: 'sre', model: 'opus', where: 'server', status: 'work', last: 'Инцидент: 502 на webapp, 2 гипотезы' },
      { id: 'coder', name: 'Кодер', kind: 'coder', model: 'sonnet', where: 'server', status: 'stop', last: 'Остановлен предохранителем: 3 одинаковые ошибки' },
      { id: 'archive', name: 'Архив', kind: 'archive', model: 'haiku', where: 'mac', status: 'idle', last: 'Ждёт файлы в ~/BotHub/inbox' },
    ];
    const threads = {
      scout: [
        { id: 's1', kind: 'sys', text: 'Начато вчера · 18:20' },
        { id: 's2', kind: 'owner', text: 'Найди свежие вакансии SRE в Европе, удалённо' },
        { id: 's3', kind: 'plan', state: 'done', secs: 38, tools: 'Chromium', dry: false, steps: [{ t: 'Открыть job-борды', state: 'done' }, { t: 'Отфильтровать по стеку и зарплате', state: 'done' }, { t: 'Сверить с резюме EN', state: 'done' }] },
        { id: 's4', kind: 'bot', text: 'Нашёл 7 вакансий SRE. Лучшая: Senior SRE, Belgrade/remote. Готов откликнуться.', foot: { tokens: '31k токенов', secs: '38 с', model: 'Sonnet 5.5' } },
        { id: 's5', kind: 'link', text: 'Жду твоего решения по отклику на «Senior SRE, Belgrade/remote».', label: 'Открыть решения', target: 'approvals', apId: 'ap-scout' },
      ],
      mac: [
        { id: 'm1', kind: 'sys', text: 'Начато на iPhone · 09:41' },
        { id: 'm2', kind: 'owner', text: 'Найди на маке PDF с договором аренды за 2025' },
        { id: 'm3', kind: 'sys', text: 'Mac спал, разбудил по сети · 11 с' },
        { id: 'm4', kind: 'plan', state: 'done', secs: 14, tools: 'Spotlight, QuickLook', dry: false, steps: [{ t: 'Spotlight: поиск по имени', state: 'done' }, { t: 'Проверить даты изменения', state: 'done' }, { t: 'Собрать превью', state: 'done' }] },
        { id: 'm5', kind: 'bot', text: 'Нашёл 3 файла, показываю два самых свежих. Самый свежий изменён 12 марта.' },
        { id: 'm6', kind: 'files', files: [
          { id: 'f1', name: 'Договор аренды 2025.pdf', path: '~/Documents/Квартира/', meta: '1,2 МБ · изменён 12 мар 2025' },
          { id: 'f2', name: 'Договор аренды 2025 (скан).pdf', path: '~/Downloads/', meta: '4,8 МБ · изменён 3 янв 2025' },
        ] },
      ],
      sre: [
        { id: 'r1', kind: 'sys', text: 'Алерт Alertmanager · 09:12' },
        { id: 'r2', kind: 'plan', state: 'done', secs: 124, tools: 'docker, dmesg, nginx', dry: false, steps: [{ t: 'docker ps и docker events', state: 'done' }, { t: 'dmesg и df -h', state: 'done' }, { t: 'Свести гипотезы', state: 'done' }] },
        { id: 'r3', kind: 'bot', text: 'Причина почти наверняка в памяти: после деплоя 08:55 контейнер упирается в лимит 1 ГБ, ядро убивает процесс 4 раза.', foot: { tokens: '58k токенов', secs: '2 мин 04 с', model: 'Opus 5.5' } },
        { id: 'r4', kind: 'link', text: 'Собрал отчёт: хронология, гипотезы и предложение исправления.', label: 'Открыть инцидент', target: 'incident' },
      ],
      coder: [
        { id: 'c1', kind: 'sys', text: 'Начато вчера · 22:05' },
        { id: 'c2', kind: 'owner', text: 'Обнови зависимости в core и прогони тесты' },
        { id: 'c3', kind: 'plan', state: 'stopped', secs: 0, tools: 'npm', dry: false, steps: [{ t: 'Прочитать lock-файл', state: 'done' }, { t: 'Обновить версии', state: 'done' }, { t: 'Прогнать тесты', state: 'wait' }] },
        { id: 'c4', kind: 'bot', text: 'Не получается: npm ERR! ERESOLVE unable to resolve dependency tree' },
        { id: 'c5', kind: 'sys', text: 'Остановлен предохранителем: 3 одинаковые ошибки' },
      ],
      archive: [
        { id: 'a1', kind: 'sys', text: 'Подписан на папку ~/BotHub/inbox' },
        { id: 'a2', kind: 'bot', text: 'Вчера разложил 4 скана по папкам. Ждёт файлы в ~/BotHub/inbox.' },
      ],
    };
    return {
      screen: 'bots', history: [], botId: 'mac', macOnline: true,
      bots, threads, runs: {}, drafts: {}, dry: false, sentCount: 0,
      approvals: [
        { id: 'ap-scout', botId: 'scout', category: 'ОТПРАВКА', title: 'Отправить отклик на «Senior SRE, Belgrade/remote»', expires: 'истекает через 27 мин',
          rows: [{ k: 'Куда', v: 'jobs.example.eu/812' }, { k: 'Данные', v: 'Резюме EN, email' }, { k: 'Отменить', v: 'Нельзя' }],
          args: 'browser.submit_form\nurl: jobs.example.eu/812 · sha 4f2a…9c1', always: false, alwaysLabel: 'Разрешать Скауту отклики на этом сайте без спроса',
          rule: 'Отклики на jobs.example.eu', done: 'Отклик отправлен. Подтверждение придёт на email.' },
        { id: 'inc', botId: 'sre', category: 'ИЗМЕНЕНИЕ СЕРВЕРА', title: 'Поднять лимит памяти до 2 ГБ и перезапустить webapp', expires: 'истекает через 12 мин', incident: true,
          rows: [{ k: 'Куда', v: 'bots · server' }, { k: 'Что', v: 'mem_limit 1g → 2g' }, { k: 'Отменить', v: 'Можно, откат за 1 мин' }],
          args: 'docker.update\nname: webapp · memory: 2g\nrestart: true', always: false, alwaysLabel: 'Разрешать SRE менять лимиты webapp без спроса',
          rule: 'Лимиты webapp', done: 'Лимит поднят до 2 ГБ, webapp перезапущен. Проверил: HTTP 200, 502 больше нет.' },
      ],
      rules: { scout: this.newRules(), mac: this.newRules(), sre: this.newRules(), coder: this.newRules(), archive: this.newRules() },
      toggles: { scout: tg(), mac: tg(), sre: tg(), coder: tg(), archive: tg() },
      usage: { claude: 42, codex: 18, gemini: 7 },
      budgets: { scout: 64, mac: 12, sre: 35, coder: 100, archive: 5 },
      guardOn: true,
      routines: [
        { id: 'ro1', group: 'sched', title: 'Проверка серверов', sub: 'SRE · каждый день 03:00', last: 'сегодня 03:00 · 2 мин · следующая через 17 ч', botId: 'sre', target: 'incident', enabled: true },
        { id: 'ro2', group: 'sched', title: 'Разбор почты', sub: 'Мак · будни 08:30', last: 'на паузе с 20 сен', botId: 'mac', target: null, enabled: false },
        { id: 'ro3', group: 'event', title: 'Алерт → диагностика', sub: 'SRE · webhook Alertmanager', last: 'последний: сегодня 09:12, 502 на webapp', botId: 'sre', target: 'incident', enabled: true },
        { id: 'ro4', group: 'event', title: 'Папка ~/BotHub/inbox', sub: 'Архив · Mac · новый файл', last: 'вчера: 4 скана разложены по папкам', botId: 'archive', target: null, enabled: true },
        { id: 'ro5', group: 'proc', title: 'Перевыпуск сертификата', sub: '4 шага · 1 подтверждение · из задачи 21 сен', last: '3 успешных запуска', botId: 'sre', target: 'procedure', enabled: true },
      ],
      toast: null,
      proc: { domain: 'bots.example.com', server: 'server', sched: false, steps: ['idle', 'idle', 'idle', 'idle'], running: false, runId: null, done: false, runs: 3, result: '' },
      incident: { decision: null, timeline: [
        { time: '09:12', text: 'Алерт: 502 на bots.example.com', danger: true },
        { time: '09:10', text: 'Контейнер webapp перезапускается в цикле', danger: false },
        { time: '09:08', text: 'Память контейнера достигла лимита 1 ГБ', danger: false },
        { time: '08:55', text: 'Деплой новой версии webapp', danger: false },
      ] },
      memory: {
        q: '',
        proposal: { text: 'Резюме EN для откликов лежит в ~/Documents/CV/Resume_EN.pdf', bot: 'Скаут', src: 'Скаут · из задачи сегодня 09:50' },
        facts: [
          { id: 'fa1', text: 'server: диск 280 ГБ, Ubuntu, Docker', src: 'SRE · 13 сен · бессрочно' },
          { id: 'fa2', text: 'Договоры по квартире: ~/Documents/Квартира', src: 'Мак · сегодня · бессрочно' },
          { id: 'fa3', text: 'Релокация: сначала Сербия, потом Япония', src: 'ты · 11 сен · до пересмотра' },
          { id: 'fa4', text: 'Сертификаты: certbot --nginx, без wildcard', src: 'SRE · 21 сен · бессрочно' },
          { id: 'fa5', text: 'Почта разбирается в 08:30 по будням, письма банка важные', src: 'Мак · 18 сен · бессрочно' },
          { id: 'fa6', text: 'Резюме и письма на английском, если вакансия не в РФ', src: 'Скаут · 14 сен · бессрочно' },
        ],
      },
      nb: { brief: '', phase: 'brief', error: false, token: null, draft: null },
    };
  }

  // ---------- хелперы state

  bot(id) { return this.S.bots.find((b) => b.id === id); }
  setBot(bots, id, patch) { return bots.map((b) => (b.id === id ? Object.assign({}, b, patch) : b)); }
  push(threads, id, msgs) {
    const add = msgs.map((m) => Object.assign({ id: this.nextId('m') }, m));
    return Object.assign({}, threads, { [id]: (threads[id] || []).concat(add) });
  }
  mapMsg(threads, id, msgId, fn) {
    return Object.assign({}, threads, { [id]: (threads[id] || []).map((m) => (m.id === msgId ? fn(m) : m)) });
  }
  haltPlans(threads, id) {
    return Object.assign({}, threads, { [id]: (threads[id] || []).map((m) => (m.kind === 'plan' && m.state === 'run'
      ? Object.assign({}, m, { state: 'stopped', steps: m.steps.map((s) => (s.state === 'run' ? Object.assign({}, s, { state: 'wait' }) : s)) })
      : m)) });
  }
  sys(text) { return { kind: 'sys', text }; }
  statusOf(S, b) {
    if (b.where === 'mac' && !S.macOnline && b.status !== 'stop') return this.K.statuses.off;
    return this.K.statuses[b.status] || this.K.statuses.idle;
  }
  without(obj, key) { const o = Object.assign({}, obj); delete o[key]; return o; }

  showToast(text, botId, label) {
    const id = this.nextId('t');
    this.commit({ toast: { id, text, botId: botId || null, label: label || '' } });
    this.after(4500, () => { if (this.S.toast && this.S.toast.id === id) this.commit({ toast: null }); });
  }

  // ---------- навигация

  go(screen, extra) {
    const S = this.S;
    this.commit(Object.assign({ screen, history: S.history.concat(S.screen) }, extra || {}));
  }
  goBack() {
    const h = this.S.history.slice();
    const prev = h.pop() || 'bots';
    this.commit({ screen: prev, history: h });
  }
  returnTo(screen, extra) {
    const S = this.S;
    const i = S.history.lastIndexOf(screen);
    this.commit(Object.assign({ screen, history: i >= 0 ? S.history.slice(0, i) : S.history }, extra || {}));
  }
  tab(screen) { this.commit({ screen, history: [] }); }
  openBot(id) { this.go('thread', { botId: id }); }
  openNewBot() { this.go('newbot', { nb: { brief: '', phase: 'brief', error: false, token: null, draft: null } }); }

  // ---------- Mac

  toggleMac() {
    const S = this.S, online = !S.macOnline;
    const patch = { macOnline: online };
    if (!online) {
      let threads = S.threads, bots = S.bots, runs = S.runs;
      S.bots.filter((b) => b.where === 'mac' && runs[b.id]).forEach((b) => {
        this.clearRun(runs[b.id]);
        runs = this.without(runs, b.id);
        threads = this.push(this.haltPlans(threads, b.id), b.id, [this.sys('Связь с Mac потеряна, задача остановлена · ' + this.hhmm())]);
        bots = this.setBot(bots, b.id, { status: 'halt', last: 'Связь с Mac потеряна' });
      });
      Object.assign(patch, { threads, bots, runs });
    }
    this.commit(patch);
  }

  // ---------- тред

  setDraft(v) { this.commit({ drafts: Object.assign({}, this.S.drafts, { [this.S.botId]: v }) }); }
  dictate() {
    const bot = this.bot(this.S.botId);
    this.setDraft(this.K.dictation[bot.kind] || 'Что нового?');
  }
  toggleDry() { this.commit({ dry: !this.S.dry }); }
  cycleModel() {
    const S = this.S, bot = this.bot(S.botId), K = this.K;
    const next = K.modelOrder[(K.modelOrder.indexOf(bot.model) + 1) % K.modelOrder.length];
    this.commit({
      bots: this.setBot(S.bots, bot.id, { model: next }),
      threads: this.push(S.threads, bot.id, [this.sys('Модель: ' + K.models[next].label)]),
    });
  }

  send() {
    const S = this.S, id = S.botId, bot = this.bot(id), tg = S.toggles[id] || {};
    const text = (S.drafts[id] || '').trim();
    if (!text || S.runs[id]) return;
    const owner = { kind: 'owner', text };
    const cleared = { drafts: Object.assign({}, S.drafts, { [id]: '' }) };
    if (bot.where === 'mac' && !S.macOnline) {
      this.commit(Object.assign({ threads: this.push(S.threads, id, [owner, this.sys('Mac не в сети. Включи Mac на экране «Боты» или переведи бота на Сервер.')]) }, cleared));
      return;
    }
    if (bot.status === 'stop' && tg.guard !== false) {
      this.commit(Object.assign({ threads: this.push(S.threads, id, [owner, this.sys('Бота держит предохранитель. Сбрось его на экране «Расход»: «Другая модель».')]) }, cleared));
      return;
    }
    this.startRun(id, { lead: [owner], extra: cleared, text, dry: S.dry, counts: true });
  }

  startRun(botId, o) {
    const S = this.S, K = this.K, bot = this.bot(botId);
    const script = K.scripts[bot.kind] || K.scripts.robot, model = K.models[bot.model];
    const runId = this.nextId('run'), planId = this.nextId('plan');
    const tg = S.toggles[botId] || {};
    if (S.runs[botId]) this.clearRun(S.runs[botId]);
    const n = S.sentCount + (o.counts ? 1 : 0);
    const due = !!o.counts && n % 3 === 0;
    const ctx = { text: o.text || '', routine: o.routine || '', resume: !!o.resume, dry: !!o.dry, n, askApproval: due && !o.dry && tg.confirm !== false, dryNote: due && !!o.dry };
    this.commit(Object.assign({
      threads: this.push(S.threads, botId, o.lead),
      bots: this.setBot(S.bots, botId, { status: 'work' }),
      runs: Object.assign({}, S.runs, { [botId]: runId }),
      sentCount: n,
    }, o.extra || {}));
    const total = script.steps.length;
    this.act(500, botId, runId, (S2) => ({
      threads: this.push(S2.threads, botId, [{ id: planId, kind: 'plan', state: 'run', secs: model.secs, tools: script.tools, dry: ctx.dry,
        steps: script.steps.map((t, i) => ({ t, state: i === 0 ? 'run' : 'wait' })) }]),
    }));
    for (let i = 0; i < total; i++) {
      this.act(1400 + i * 900, botId, runId, (S2) => {
        const last = i === total - 1;
        const threads = this.mapMsg(S2.threads, botId, planId, (m) => Object.assign({}, m, {
          state: last ? 'done' : 'run',
          steps: m.steps.map((s, k) => Object.assign({}, s, { state: k <= i ? 'done' : (k === i + 1 ? 'run' : 'wait') })),
        }));
        return last ? this.finishRun(S2, botId, threads, ctx) : { threads };
      });
    }
  }

  act(ms, botId, runId, fn) {
    this.after(ms, () => {
      const S = this.S;
      if (S.runs[botId] !== runId) return;
      const patch = fn(S);
      if (patch) this.commit(patch);
    }, runId);
  }

  finishRun(S, botId, threads, ctx) {
    const K = this.K, bot = this.bot(botId), model = K.models[bot.model], script = K.scripts[bot.kind] || K.scripts.robot;
    const pre = ctx.dry ? 'Пробный прогон: ничего не менял. ' : '';
    const intro = ctx.resume ? 'Продолжил с места, где ты передал управление. ' : ctx.routine ? 'Рутина «' + ctx.routine + '» выполнена. ' : (ctx.text ? 'Принял: «' + this.short(ctx.text) + '». ' : '');
    const reply = { kind: 'bot', text: pre + intro + script.reply, foot: { tokens: (model.tokens + (ctx.n % 4)) + 'k токенов', secs: this.fmtSecs(model.secs), model: model.label } };
    const msgs = [reply];
    let approvals = S.approvals, last = reply.text;
    if (ctx.askApproval) {
      const ap = this.makeApproval(bot, ctx.n);
      approvals = S.approvals.concat(ap);
      msgs.push({ kind: 'link', text: 'Нужно твоё решение: ' + ap.title, label: 'Открыть решения', target: 'approvals', apId: ap.id });
      last = 'Жду решения: ' + ap.title;
    }
    if (ctx.dryNote) msgs.push(this.sys('Пробный прогон: в реальном запуске бот здесь попросил бы подтверждение'));
    const pending = approvals.some((a) => a.botId === botId);
    return {
      threads: this.push(threads, botId, msgs),
      bots: this.setBot(S.bots, botId, { status: pending ? 'wait' : 'ok', last }),
      runs: this.without(S.runs, botId),
      approvals,
      usage: Object.assign({}, S.usage, { claude: Math.min(100, S.usage.claude + model.quota) }),
      budgets: Object.assign({}, S.budgets, { [botId]: Math.min(100, (S.budgets[botId] || 0) + model.quota) }),
    };
  }

  makeApproval(bot, n) {
    const pools = {
      scout: [{ category: 'ОТПРАВКА', title: 'Отправить отклик на «Platform Engineer, Tallinn»', rows: [['Куда', 'jobs.example.ee/204'], ['Данные', 'Резюме EN, email'], ['Отменить', 'Нельзя']], args: 'browser.submit_form\nurl: jobs.example.ee/204', rule: 'Отклики на jobs.example.ee', done: 'Отклик отправлен.' }],
      mac: [{ category: 'УДАЛЕНИЕ', title: 'Удалить 12 дубликатов из ~/Downloads', rows: [['Где', '~/Downloads'], ['Объём', '340 МБ'], ['Отменить', 'Корзина, 30 дней']], args: 'fs.trash\npath: ~/Downloads · files: 12', rule: 'Чистка ~/Downloads', done: 'Перенёс 12 дубликатов в Корзину. Освободилось 340 МБ.' }],
      sre: [{ category: 'ИЗМЕНЕНИЕ СЕРВЕРА', title: 'Перезапустить nginx на сервере', rows: [['Куда', 'server'], ['Что', 'systemctl restart nginx'], ['Отменить', 'Нельзя']], args: 'ssh.exec\nhost: server\ncmd: systemctl restart nginx', rule: 'Перезапуск nginx', done: 'nginx перезапущен, проверка HTTPS прошла.' }],
      coder: [{ category: 'ОТПРАВКА', title: 'Запушить 3 коммита в ветку ai/fix-deps', rows: [['Куда', 'github.com/example/botstead'], ['Что', '3 коммита, 7 файлов'], ['Отменить', 'Можно, revert']], args: 'git.push\nbranch: ai/fix-deps · commits: 3', rule: 'Пуш в ai/*', done: 'Запушил ветку ai/fix-deps.' }],
      archive: [{ category: 'ПЕРЕМЕЩЕНИЕ', title: 'Переместить 4 скана в ~/Documents/Договоры', rows: [['Откуда', '~/BotHub/inbox'], ['Куда', '~/Documents/Договоры'], ['Отменить', 'Можно']], args: 'fs.move\nfrom: ~/BotHub/inbox · files: 4', rule: 'Раскладка сканов', done: 'Разложил 4 скана по папкам.' }],
    };
    const generic = [{ category: 'ДЕЙСТВИЕ', title: 'Применить изменения от имени бота «' + bot.name + '»', rows: [['Куда', 'рабочая папка бота'], ['Что', 'запись файлов'], ['Отменить', 'Можно']], args: 'fs.write\nbot: ' + bot.id, rule: 'Запись в рабочую папку', done: 'Изменения применены.' }];
    const pool = pools[bot.kind] || generic;
    const p = pool[Math.floor(n / 3) % pool.length];
    return {
      id: this.nextId('ap'), botId: bot.id, category: p.category, title: p.title, expires: 'истекает через 30 мин',
      rows: p.rows.map((r) => ({ k: r[0], v: r[1] })), args: p.args, always: false,
      alwaysLabel: 'Разрешать «' + bot.name + '» такие действия без спроса', rule: p.rule, done: p.done,
    };
  }

  stop() {
    const S = this.S, id = S.botId, runId = S.runs[id];
    if (!runId) return;
    const plan = (S.threads[id] || []).slice().reverse().find((m) => m.kind === 'plan' && m.state === 'run');
    const done = plan ? plan.steps.filter((s) => s.state === 'done').length : 0;
    const total = plan ? plan.steps.length : 3;
    const note = plan ? 'Остановлено владельцем · шаг ' + Math.min(done + 1, total) + ' из ' + total + ' · ' + this.hhmm() : 'Остановлено владельцем до начала работы · ' + this.hhmm();
    this.clearRun(runId);
    this.commit({
      threads: this.push(this.haltPlans(S.threads, id), id, [this.sys(note)]),
      runs: this.without(S.runs, id),
      bots: this.setBot(S.bots, id, { status: 'halt', last: 'Остановлено владельцем' }),
    });
  }

  // ---------- решения

  toggleAlways(id) {
    this.commit({ approvals: this.S.approvals.map((a) => (a.id === id ? Object.assign({}, a, { always: !a.always }) : a)) });
  }
  editApproval(a) {
    const S = this.S;
    this.go('thread', { botId: a.botId, drafts: Object.assign({}, S.drafts, { [a.botId]: 'Измени условия: ' }) });
  }
  resolveApproval(id, approve) {
    const S = this.S, ap = S.approvals.find((a) => a.id === id);
    if (!ap) return;
    const approvals = S.approvals.filter((a) => a.id !== id);
    const lead = [this.sys((approve ? 'Разрешено' : 'Отклонено') + ': ' + ap.title + ' · ' + this.hhmm())];
    let rules = S.rules;
    if (approve && ap.always) {
      rules = Object.assign({}, rules, { [ap.botId]: (rules[ap.botId] || []).concat({ id: this.nextId('r'), label: ap.rule, auto: true }) });
      lead.push(this.sys('Правило добавлено: ' + ap.rule + ' · без спроса'));
    }
    const patch = {
      approvals, rules,
      threads: this.push(S.threads, ap.botId, lead),
      bots: this.setBot(S.bots, ap.botId, { status: S.runs[ap.botId] ? 'work' : (approvals.some((a) => a.botId === ap.botId) ? 'wait' : 'ok') }),
    };
    if (ap.incident) {
      patch.incident = Object.assign({}, S.incident, {
        decision: approve ? 'approved' : 'rejected',
        timeline: [{ time: this.hhmm(), text: approve ? 'Лимит памяти поднят до 2 ГБ, webapp перезапущен' : 'Предложение отклонено владельцем', danger: false }].concat(S.incident.timeline),
      });
    }
    this.commit(patch);
    this.after(900, () => {
      const S2 = this.S;
      const text = approve ? ap.done : 'Понял, отменяю. Ничего не изменено.';
      this.commit({ threads: this.push(S2.threads, ap.botId, [{ kind: 'bot', text }]), bots: this.setBot(S2.bots, ap.botId, { last: text }) });
    });
  }

  // ---------- handoff

  handoffBack() {
    const S = this.S, bot = this.bot(S.botId);
    const back = this.sys('Управление возвращено боту · ' + this.hhmm());
    if (S.runs[bot.id] || (bot.where === 'mac' && !S.macOnline)) {
      const off = !S.runs[bot.id];
      this.returnTo('thread', { threads: this.push(S.threads, bot.id, off ? [back, this.sys('Mac не в сети: бот не продолжил работу')] : [back]) });
      return;
    }
    this.returnTo('thread', {});
    this.startRun(bot.id, { lead: [back], resume: true, dry: false, counts: false });
  }
  handoffStop() {
    const S = this.S, bot = this.bot(S.botId);
    let threads = S.threads, runs = S.runs;
    if (runs[bot.id]) { this.clearRun(runs[bot.id]); runs = this.without(runs, bot.id); threads = this.haltPlans(threads, bot.id); }
    this.returnTo('thread', {
      threads: this.push(threads, bot.id, [this.sys('Задача остановлена на экране · ' + this.hhmm())]),
      bots: this.setBot(S.bots, bot.id, { status: 'halt', last: 'Остановлено владельцем' }),
      runs,
    });
  }

  // ---------- рутины

  addRoutine() {
    const S = this.S, n = S.routines.filter((r) => r.title.indexOf('Новая рутина') === 0).length + 1;
    const r = { id: this.nextId('ro'), group: 'sched', title: n > 1 ? 'Новая рутина ' + n : 'Новая рутина', sub: 'Мак · каждый день 09:00', last: 'ещё не запускалась', botId: 'mac', target: null, enabled: true };
    this.commit({ routines: S.routines.concat(r) });
    this.showToast('Рутина добавлена в расписание');
  }
  openRoutine(r) {
    if (r.target === 'incident') this.go('incident');
    else if (r.target === 'procedure') this.go('procedure');
    else this.openBot(r.botId);
  }
  toggleRoutine(id) {
    const S = this.S;
    this.commit({ routines: S.routines.map((r) => (r.id !== id ? r : Object.assign({}, r, {
      enabled: !r.enabled,
      last: r.enabled ? 'на паузе с сегодня ' + this.hhmm() : 'включена, ждёт следующего срабатывания',
    }))) });
  }
  runRoutine(r) {
    const S = this.S, bot = this.bot(r.botId);
    if (!bot) return;
    if (S.runs[bot.id]) { this.showToast('«' + bot.name + '» занят: дождись конца текущей задачи'); return; }
    if (bot.where === 'mac' && !S.macOnline) { this.showToast('Mac не в сети: рутина не запущена'); return; }
    this.startRun(bot.id, {
      lead: [this.sys('Рутина «' + r.title + '» запущена вручную · ' + this.hhmm())], routine: r.title, dry: false, counts: false,
      extra: { routines: S.routines.map((x) => (x.id === r.id ? Object.assign({}, x, { last: 'только что · запущена вручную' }) : x)) },
    });
    this.showToast('Рутина запущена в треде: ' + bot.name, bot.id, 'Открыть');
  }

  // ---------- процедура

  toggleSched() { this.commit({ proc: Object.assign({}, this.S.proc, { sched: !this.S.proc.sched }) }); }
  runProc() {
    const S = this.S;
    if (S.proc.running) return;
    const runId = this.nextId('pr');
    const total = 4;
    this.commit({ proc: Object.assign({}, S.proc, { running: true, runId, done: false, steps: ['run', 'idle', 'idle', 'idle'] }) });
    for (let i = 0; i < total; i++) {
      this.after(900 * (i + 1), () => {
        const S2 = this.S;
        if (S2.proc.runId !== runId) return;
        const last = i === total - 1;
        const steps = S2.proc.steps.map((s, k) => (k <= i ? 'done' : (k === i + 1 ? 'run' : 'idle')));
        if (!last) { this.commit({ proc: Object.assign({}, S2.proc, { steps }) }); return; }
        this.commit({
          proc: Object.assign({}, S2.proc, { steps, running: false, done: true, runs: S2.proc.runs + 1, result: 'Готово: HTTP 200, сертификат продлён на 90 дней' }),
          threads: this.push(S2.threads, 'sre', [this.sys('Процедура «Перевыпуск сертификата» выполнена · ' + this.hhmm())]),
        });
      });
    }
  }

  // ---------- расход

  swapGuardModel() {
    const S = this.S;
    this.commit({
      guardOn: false,
      bots: this.setBot(S.bots, 'coder', { model: 'opus', status: 'ok', last: 'Предохранитель сброшен, модель Opus 5.5' }),
      budgets: Object.assign({}, S.budgets, { coder: 60 }),
      threads: this.push(S.threads, 'coder', [this.sys('Модель сменена на Opus 5.5, предохранитель сброшен · ' + this.hhmm())]),
    });
  }

  // ---------- память

  rememberProposal() {
    const m = this.S.memory;
    if (!m.proposal) return;
    const fact = { id: this.nextId('fa'), text: m.proposal.text, src: m.proposal.bot + ' · сегодня · бессрочно' };
    this.commit({ memory: Object.assign({}, m, { proposal: null, facts: [fact].concat(m.facts) }) });
    this.showToast('Запомнил');
  }
  dismissProposal() { this.commit({ memory: Object.assign({}, this.S.memory, { proposal: null }) }); }
  removeFact(id) { this.commit({ memory: Object.assign({}, this.S.memory, { facts: this.S.memory.facts.filter((f) => f.id !== id) }) }); }

  // ---------- настройки бота

  setBotField(patch) { const S = this.S; this.commit({ bots: this.setBot(S.bots, S.botId, patch) }); }
  nextPersona() {
    const K = this.K, bot = this.bot(this.S.botId);
    this.setBotField({ kind: K.personaOrder[(K.personaOrder.indexOf(bot.kind) + 1) % K.personaOrder.length] });
  }
  flipToggle(key) {
    const S = this.S, id = S.botId, cur = S.toggles[id] || { full: true, confirm: true, guard: true };
    this.commit({ toggles: Object.assign({}, S.toggles, { [id]: Object.assign({}, cur, { [key]: !cur[key] }) }) });
  }
  removeChip(chipId) {
    const S = this.S, id = S.botId;
    this.commit({ rules: Object.assign({}, S.rules, { [id]: (S.rules[id] || []).filter((r) => r.id !== chipId) }) });
  }

  // ---------- новый бот

  guess(brief) {
    const t = brief.toLowerCase();
    const has = (re) => re.test(t);
    let kind = 'robot', name = 'Помощник';
    if (has(/почт|письм|ящик/)) { kind = 'owl'; name = 'Секретарь'; }
    else if (has(/код|тест|репо|git|коммит/)) { kind = 'coder'; name = 'Ревьюер'; }
    else if (has(/сервер|алерт|лог|деплой|docker/)) { kind = 'sre'; name = 'Дежурный'; }
    else if (has(/файл|папк|скан|документ/)) { kind = 'archive'; name = 'Библиотекарь'; }
    else if (has(/вакан|работ|поиск|найд|ищи/)) { kind = 'scout'; name = 'Охотник'; }
    else if (has(/иде|текст|пост|новост|сводк/)) { kind = 'spark'; name = 'Идеолог'; }
    const model = has(/сложн|архитект|разбор/) ? 'opus' : (has(/простой|быстр|мелк/) ? 'haiku' : 'sonnet');
    const where = has(/\bmac\b|мак|файл|папк|скан|приложени/) ? 'mac' : 'server';
    return { name, kind, model, where, desc: this.K.personas[kind].desc, schedule: this.guessSchedule(brief, t) };
  }
  // Регулярность из описания: cron + человекочитаемая подпись. Нет слов про повтор: null.
  guessSchedule(brief, t) {
    if (!/кажд(ый|ое|ую|ые|ому)|ежедневн|ежечасн|утром|вечером|по будням|по (понедельник|вторник|сред|четверг|пятниц|суббот|воскресень)|раз в/.test(t)) return null;
    const every = t.match(/кажд(?:ые|ый)\s+(\d{1,2})?\s*(минут|час)/);
    if (/ежечасн/.test(t) || (every && every[2] === 'час' && !every[1])) return { name: this.short(brief.charAt(0).toUpperCase() + brief.slice(1)), cron: '0 * * * *', human: 'каждый час', enabled: true };
    if (every && every[1]) { const n = Math.max(1, Math.min(59, +every[1])); const c = every[2] === 'минут' ? '*/' + n + ' * * * *' : '0 */' + Math.min(23, n) + ' * * *'; return { name: this.short(brief.charAt(0).toUpperCase() + brief.slice(1)), cron: c, human: 'каждые ' + n + (every[2] === 'минут' ? ' минут' : ' часов'), enabled: true }; }
    const tm = t.match(/(?:^|\s)[вк]\s*(\d{1,2})(?:[:.](\d{2}))?(?=\s|$|\s*(?:утра|вечера|часов|ч\b))/);
    const pm = tm && /вечера/.test(t.slice(tm.index, tm.index + tm[0].length + 12));
    const hh = tm ? Math.min(23, +tm[1] + (pm && +tm[1] < 12 ? 12 : 0)) : (/вечер/.test(t) ? 18 : 9), mm = tm && tm[2] ? Math.min(59, +tm[2]) : 0;
    const at = String(hh).padStart(2, '0') + ':' + String(mm).padStart(2, '0');
    const cron = (dom, dow) => mm + ' ' + hh + ' ' + dom + ' * ' + dow;
    const days = [['понедельник', 1, 'по понедельникам'], ['вторник', 2, 'по вторникам'], ['сред', 3, 'по средам'], ['четверг', 4, 'по четвергам'], ['пятниц', 5, 'по пятницам'], ['суббот', 6, 'по субботам'], ['воскресень', 0, 'по воскресеньям']];
    let c, human;
    const day = days.find((d) => t.indexOf(d[0]) >= 0);
    if (/будн/.test(t)) { c = cron('*', '1-5'); human = 'по будням в ' + at; }
    else if (day) { c = cron('*', day[1]); human = day[2] + ' в ' + at; }
    else if (/раз в недел/.test(t)) { c = cron('*', 1); human = 'раз в неделю, по понедельникам в ' + at; }
    else if (/раз в месяц/.test(t)) { c = cron('1', '*'); human = 'раз в месяц, 1-го числа в ' + at; }
    else { c = cron('*', '*'); human = 'каждый день в ' + at; }
    const name = brief.replace(/(^|\s)[вк]\s*\d{1,2}(?:[:.]\d{2})?(\s*(утра|вечера|часов))?/gi, ' ').replace(/\s+/g, ' ').trim().replace(/^(каждый день|каждое утро|каждый вечер|каждую неделю|ежедневно|по будням|по [а-яё]+(ам|ям)|каждый [а-яё]+|раз в [а-яё]+|утром|вечером)[,\s]+/i, '').trim() || brief;
    return { name: this.short(name.charAt(0).toUpperCase() + name.slice(1)), cron: c, human, enabled: true };
  }
  buildBot() {
    const S = this.S, nb = S.nb;
    if (nb.phase === 'building') return;
    const brief = nb.brief.trim();
    if (brief.length < 10) { this.commit({ nb: Object.assign({}, nb, { error: true }) }); return; }
    const token = this.nextId('nb');
    this.commit({ nb: Object.assign({}, nb, { phase: 'building', error: false, token }) });
    this.after(1500, () => {
      const S2 = this.S;
      if (S2.nb.token !== token || S2.nb.phase !== 'building') return;
      this.commit({ nb: Object.assign({}, S2.nb, { phase: 'draft', draft: this.guess(brief) }) });
    });
  }
  toggleSchedule() {
    const d = this.S.nb.draft;
    if (!d || !d.schedule) return;
    this.setDraftField({ schedule: Object.assign({}, d.schedule, { enabled: !d.schedule.enabled }) });
  }
  createBot() {
    const S = this.S, d = S.nb.draft;
    if (!d) return;
    const id = this.nextId('bot');
    const name = d.name.trim() || 'Новый бот';
    const bot = { id, name, kind: d.kind, model: d.model, where: d.where, status: 'idle', last: 'Создан только что' };
    const sch = d.schedule && d.schedule.enabled ? d.schedule : null;
    const routine = sch ? { id: this.nextId('ro'), group: 'sched', title: sch.name, sub: name + ' · ' + sch.human, last: 'ещё не запускалась', botId: id, target: null, enabled: true } : null;
    this.commit({
      bots: S.bots.concat(bot),
      routines: routine ? S.routines.concat(routine) : S.routines,
      threads: Object.assign({}, S.threads, { [id]: [
        { id: id + 'a', kind: 'sys', text: 'Бот создан · ' + this.hhmm() + (sch ? ' · расписание: ' + sch.human : '') },
        { id: id + 'b', kind: 'bot', text: 'Привет! Я ' + name + '. ' + d.desc + '. Напиши, с чего начать.' },
      ] }),
      rules: Object.assign({}, S.rules, { [id]: this.newRules() }),
      toggles: Object.assign({}, S.toggles, { [id]: { full: true, confirm: true, guard: true } }),
      budgets: Object.assign({}, S.budgets, { [id]: 0 }),
      botId: id, screen: 'thread', history: ['bots'],
      nb: { brief: '', phase: 'brief', error: false, token: null, draft: null },
    });
    if (routine) this.showToast('Рутина «' + routine.title + '» добавлена в расписание');
  }
  setDraftField(patch) { const nb = this.S.nb; this.commit({ nb: Object.assign({}, nb, { draft: Object.assign({}, nb.draft, patch) }) }); }

  // ---------- рендер

  renderVals() {
    const S = this.state, K = this.K;
    const bot = S.bots.find((b) => b.id === S.botId) || S.bots[0];
    const kf = (kind) => ({ isScout: kind === 'scout', isMac: kind === 'mac', isSre: kind === 'sre', isCoder: kind === 'coder', isArchive: kind === 'archive', isOwl: kind === 'owl', isSpark: kind === 'spark', isRobot: kind === 'robot' });
    const botView = (b) => {
      const st = this.statusOf(S, b);
      return Object.assign({ id: b.id, name: b.name, modelLabel: K.models[b.model].label, whereLabel: b.where === 'mac' ? 'Mac' : 'Сервер', statusText: st[0], dotColor: 'var(--' + st[1] + ')', last: b.last }, kf(b.kind));
    };
    const cur = botView(bot);
    const curSt = this.statusOf(S, bot);
    cur.sub = K.models[bot.model].short + ' · ' + (bot.where === 'mac' ? 'Mac' : 'Сервер') + ' · ' + curSt[0];
    cur.desc = K.personas[bot.kind].desc;
    cur.draft = S.drafts[bot.id] || '';
    cur.screenSub = bot.where === 'mac' ? 'MacBook Air · экран' : 'Контейнер bot-' + bot.id + ' · Chromium';
    const running = !!S.runs[bot.id];
    const msgs = (S.threads[bot.id] || []).map((m) => {
      const o = {
        id: m.id, text: m.text || '', isSys: m.kind === 'sys', isOwner: m.kind === 'owner', isBot: m.kind === 'bot', isLink: m.kind === 'link', isPlan: m.kind === 'plan', isFiles: m.kind === 'files',
        hasFoot: !!m.foot, foot: m.foot || { tokens: '', secs: '', model: '' }, label: m.label || '',
        openLink: () => (m.target === 'incident' ? this.go('incident') : this.tab('approvals')),
        hasAp: !!m.apId && S.approvals.some((a) => a.id === m.apId),
        apApprove: () => this.resolveApproval(m.apId, true), apReject: () => this.resolveApproval(m.apId, false),
        planRun: false, planDone: false, planStopped: false, planVerified: false, planDry: false, planTitle: '', steps: [], files: [],
      };
      if (m.kind === 'plan') {
        const nDone = m.steps.filter((s) => s.state === 'done').length;
        o.planRun = m.state === 'run'; o.planDone = m.state === 'done'; o.planStopped = m.state === 'stopped';
        o.planVerified = o.planDone && !m.dry; o.planDry = o.planDone && !!m.dry;
        o.planTitle = o.planRun ? 'План: ' + m.steps.length + ' шага · шаг ' + Math.min(nDone + 1, m.steps.length)
          : (o.planDone ? m.steps.length + ' шага · ' + this.fmtSecs(m.secs) + ' · ' + m.tools : 'Остановлено на шаге ' + Math.min(nDone + 1, m.steps.length));
        o.steps = m.steps.map((s) => ({ t: s.t, isDone: s.state === 'done', isRun: s.state === 'run', isWait: s.state === 'wait', color: s.state === 'wait' ? 'var(--fg-muted)' : 'var(--fg-default)' }));
      }
      if (m.kind === 'files') {
        const note = (t) => () => this.commit({ threads: this.push(this.S.threads, bot.id, [this.sys(t)]) });
        o.files = m.files.map((f) => ({ name: f.name, path: f.path, meta: f.meta,
          preview: note('Превью на экране: ' + f.name), attach: note('Файл добавлен в тред: ' + f.name), download: note('Скачано в Загрузки: ' + f.name) }));
      }
      return o;
    });
    const runPlan = (S.threads[bot.id] || []).slice().reverse().find((m) => m.kind === 'plan' && m.state === 'run');
    const runLabel = runPlan ? 'Работает · шаг ' + Math.min(runPlan.steps.filter((s) => s.state === 'done').length + 1, runPlan.steps.length) + ' из ' + runPlan.steps.length : 'Работает · составляю план';

    const tabItem = (key) => ({ open: () => this.tab(key), color: S.screen === key ? 'var(--fg-default)' : 'var(--fg-muted)', current: S.screen === key ? 'page' : 'false' });
    const n = S.approvals.length;
    const meters = (list) => list.map((m) => ({ name: m.name, pct: m.pct, pctText: m.pct + '%', width: Math.min(m.pct, 100) + '%', color: 'var(--' + m.color + ')', pctColor: m.pct >= 100 ? 'var(--danger-fg)' : 'var(--fg-muted)' }));
    const seg = (items, active, pick) => items.map((it) => ({ label: it.label, checked: it.value === active ? 'true' : 'false', bg: it.value === active ? 'var(--bg-surface)' : 'transparent', weight: it.value === active ? '600' : '500', pick: () => pick(it.value) }));
    const modelItems = K.modelOrder.map((k) => ({ label: K.models[k].short, value: k }));
    const whereItems = [{ label: 'Сервер', value: 'server' }, { label: 'Mac', value: 'mac' }];
    const tg = S.toggles[bot.id] || { full: true, confirm: true, guard: true };
    const rules = S.rules[bot.id] || [];
    const chip = (r) => ({ label: r.label, remove: () => this.removeChip(r.id) });
    const personas = {};
    K.personaOrder.forEach((k) => { personas[k] = { pick: () => this.setBotField({ kind: k }), border: bot.kind === k ? 'var(--fg-default)' : 'transparent', pressed: bot.kind === k ? 'true' : 'false' }; });

    const proc = S.proc;
    const procStepText = { idle: '', run: 'выполняется…', done: 'готово' };
    const steps = ['Проверить срок сертификата и DNS', 'certbot certonly --nginx для домена', 'Перезагрузить nginx', 'Проверить HTTPS и дату нового сертификата'].map((text, i) => {
      const st = proc.steps[i];
      return { n: i + 1, text, hasApproval: i === 2, isDone: st === 'done', notDone: st !== 'done', stateText: procStepText[st],
        bg: st === 'done' ? 'var(--success-fg)' : (st === 'run' ? 'var(--attention-bg)' : 'var(--bg-sunken)'), fg: st === 'done' ? 'var(--bg-surface)' : (st === 'run' ? 'var(--attention-text)' : 'var(--fg-default)') };
    });

    const inc = S.incident;
    const incAp = S.approvals.some((a) => a.id === 'inc');
    const incApproved = inc.decision === 'approved';
    const mem = S.memory, q = mem.q.trim().toLowerCase();
    const facts = mem.facts.filter((f) => !q || f.text.toLowerCase().indexOf(q) >= 0 || f.src.toLowerCase().indexOf(q) >= 0);
    const nb = S.nb, d = nb.draft;
    const canBuild = nb.brief.trim().length >= 10;
    const routineView = (g) => S.routines.filter((r) => r.group === g).map((r) => ({
      title: r.title, sub: r.sub, last: r.last, enabled: r.enabled, paused: !r.enabled,
      open: () => this.openRoutine(r), run: () => this.runRoutine(r), toggle: () => this.toggleRoutine(r.id),
    }));
    const macSt = S.macOnline ? ['В сети', 'success-fg', 'Полный контроль · Tailscale · 21 мс'] : ['Не в сети', 'danger-fg', 'Mac спит или нет сети · последний раз 12 мин назад'];

    return {
      theme: this.props.theme ?? 'light',
      isBots: S.screen === 'bots', isThread: S.screen === 'thread', isApprovals: S.screen === 'approvals', isHandoff: S.screen === 'handoff',
      isRoutines: S.screen === 'routines', isProcedure: S.screen === 'procedure', isIncident: S.screen === 'incident', isUsage: S.screen === 'usage',
      isMemory: S.screen === 'memory', isSettings: S.screen === 'settings', isNewBot: S.screen === 'newbot',
      showTabs: ['bots', 'routines', 'approvals', 'usage'].indexOf(S.screen) >= 0,
      tab: { bots: tabItem('bots'), routines: tabItem('routines'), approvals: tabItem('approvals'), usage: tabItem('usage') },
      tabDot: n > 0,
      hasToast: !!S.toast,
      toast: S.toast ? { text: S.toast.text, hasOpen: !!S.toast.botId, label: S.toast.label, open: () => { this.openBot(S.toast.botId); } } : { text: '', hasOpen: false, label: '', open: () => {} },
      goBack: () => this.goBack(),
      cur,
      procBot: kf(S.bots.find((b) => b.id === 'sre') ? S.bots.find((b) => b.id === 'sre').kind : 'sre'),
      // Боты
      macCard: { toggle: () => this.toggleMac(), label: macSt[0], dot: 'var(--' + macSt[1] + ')', sub: macSt[2] },
      bannerShow: n > 0,
      bannerText: n + ' ' + this.plural(n, ['действие ждёт', 'действия ждут', 'действий ждут']) + ' решения',
      goApprovals: () => this.tab('approvals'),
      bots: S.bots.map((b) => Object.assign(botView(b), { open: () => this.openBot(b.id) })),
      openMemory: () => this.go('memory'),
      openSettings: () => this.go('settings'),
      openNewBot: () => this.openNewBot(),
      // Тред
      thread: { rev: msgs.slice().reverse(), running, idle: !running, runLabel },
      dry: S.dry,
      toggleDry: () => this.toggleDry(),
      cycleModel: () => this.cycleModel(),
      onDraft: (e) => this.setDraft(e.target.value),
      onDraftKey: (e) => { if (e.key === 'Enter') { e.preventDefault(); this.send(); } },
      dictate: () => this.dictate(),
      send: () => this.send(),
      stop: () => this.stop(),
      openHandoff: () => this.go('handoff'),
      // Решения
      ap: {
        empty: n === 0,
        items: S.approvals.map((a) => Object.assign({
          botName: this.bot(a.botId).name, expires: a.expires, category: a.category, title: a.title, rows: a.rows, args: a.args,
          always: a.always, alwaysLabel: a.alwaysLabel, toggleAlways: () => this.toggleAlways(a.id),
          approve: () => this.resolveApproval(a.id, true), reject: () => this.resolveApproval(a.id, false), edit: () => this.editApproval(a),
        }, kf(this.bot(a.botId).kind))),
      },
      // Handoff
      hand: { title: 'Экран · ' + bot.name, giveBack: () => this.handoffBack(), stop: () => this.handoffStop() },
      // Рутины
      rt: { sched: routineView('sched'), event: routineView('event'), proc: routineView('proc') },
      addRoutine: () => this.addRoutine(),
      // Процедура
      proc: {
        domain: proc.domain, server: proc.server,
        onDomain: (e) => this.commit({ proc: Object.assign({}, this.S.proc, { domain: e.target.value }) }),
        onServer: (e) => this.commit({ proc: Object.assign({}, this.S.proc, { server: e.target.value }) }),
        steps, sched: proc.sched, notSched: !proc.sched, toggleSched: () => this.toggleSched(),
        running: proc.running, run: () => this.runProc(), runOpacity: proc.running ? '0.6' : '1',
        runLabel: proc.running ? 'Выполняется…' : (proc.done ? 'Запустить ещё раз' : 'Запустить'),
        hasResult: proc.done, result: proc.result,
        info: proc.runs + ' ' + this.plural(proc.runs, ['успешный запуск', 'успешных запуска', 'успешных запусков']) + ' · проверка: HTTP 200 и дата сертификата' + (proc.sched ? ' · по расписанию: 1-го числа в 03:00' : ''),
      },
      // Инцидент
      inc: {
        sub: 'SRE · ' + K.models[(S.bots.find((b) => b.id === 'sre') || bot).model].label + ' · из алерта 09:12',
        chipText: incApproved ? 'РЕШЕНО' : 'КРИТИЧНО', chipBg: incApproved ? 'var(--bg-sunken)' : 'var(--danger-bg)', chipFg: incApproved ? 'var(--success-fg)' : 'var(--danger-fg)',
        timeline: inc.timeline.map((t) => ({ time: t.time, text: t.text, color: t.danger ? 'var(--danger-fg)' : 'var(--fg-muted)' })),
        pending: incAp, decided: !incAp,
        decisionText: incApproved ? 'Разрешено: лимит поднят до 2 ГБ, webapp перезапущен' : 'Отклонено: лимит остаётся 1 ГБ',
        decisionBg: incApproved ? 'var(--bg-surface)' : 'var(--bg-sunken)', decisionFg: incApproved ? 'var(--success-fg)' : 'var(--fg-muted)',
        approve: () => this.resolveApproval('inc', true), reject: () => this.resolveApproval('inc', false),
      },
      // Расход
      usage: {
        subs: meters([{ name: 'Claude', pct: S.usage.claude, color: 'claude-fg' }, { name: 'Codex', pct: S.usage.codex, color: 'codex-fg' }, { name: 'Gemini', pct: S.usage.gemini, color: 'gemini-fg' }]),
        budgets: meters(S.bots.map((b) => ({ name: b.name, pct: S.budgets[b.id] || 0, color: (S.budgets[b.id] || 0) >= 100 ? 'danger-fg' : 'claude-fg' }))),
        guardOn: S.guardOn, guardOff: !S.guardOn, guardText: 'Предохранитель сброшен: Кодер снова работает на Opus 5.5',
        openThread: () => this.openBot('coder'), swapModel: () => this.swapGuardModel(),
      },
      // Память
      mem: {
        q: mem.q, onQ: (e) => this.commit({ memory: Object.assign({}, this.S.memory, { q: e.target.value }) }),
        sub: 'Общая для всех ботов · ' + mem.facts.length + ' ' + this.plural(mem.facts.length, ['факт', 'факта', 'фактов']),
        hasProposal: !!mem.proposal, proposal: mem.proposal || { text: '', src: '' },
        remember: () => this.rememberProposal(), dismiss: () => this.dismissProposal(),
        facts: facts.map((f) => ({ text: f.text, src: f.src, remove: () => this.removeFact(f.id) })), noFacts: facts.length === 0,
      },
      // Настройки
      nextPersona: () => this.nextPersona(),
      personas,
      set: {
        modelOpts: seg(modelItems, bot.model, (v) => this.setBotField({ model: v })),
        whereOpts: seg(whereItems, bot.where, (v) => this.setBotField({ where: v })),
        toggles: [
          { id: 'tg-full', label: 'Полный контроль Mac', sub: 'Файлы, приложения, клики и ввод, скриншоты', on: tg.full, toggle: () => this.flipToggle('full') },
          { id: 'tg-confirm', label: 'Подтверждать отправки', sub: 'Письма, отклики и формы только после твоего решения', on: tg.confirm, toggle: () => this.flipToggle('confirm') },
          { id: 'tg-guard', label: 'Предохранитель', sub: 'Стоп после 3 одинаковых ошибок, до 30 мин на задачу', on: tg.guard, toggle: () => this.flipToggle('guard') },
        ],
        autoChips: rules.filter((r) => r.auto).map(chip), askChips: rules.filter((r) => !r.auto).map(chip),
        noAuto: !rules.some((r) => r.auto), noAsk: !rules.some((r) => !r.auto),
      },
      // Новый бот
      nb: {
        brief: nb.brief, onBrief: (e) => this.commit({ nb: Object.assign({}, this.S.nb, { brief: e.target.value, error: false }) }),
        hasError: nb.error, building: nb.phase === 'building', isDraft: nb.phase === 'draft',
        canBuild, buildDisabled: !canBuild || nb.phase === 'building',
        showHint: !canBuild, hintColor: nb.error ? 'var(--danger-fg)' : 'var(--fg-muted)',
        buildLabel: nb.phase === 'building' ? 'Собираю…' : 'Собрать', buildOpacity: (!canBuild || nb.phase === 'building') ? '0.5' : '1',
        hasSchedule: !!(d && d.schedule), toggleSchedule: () => this.toggleSchedule(),
        sch: d && d.schedule ? { human: d.schedule.human, cron: d.schedule.cron, on: d.schedule.enabled, off: !d.schedule.enabled } : { human: '', cron: '', on: false, off: false },
        build: () => this.buildBot(),
        example: () => this.commit({ nb: Object.assign({}, this.S.nb, { brief: 'Каждое утро собирай новости про Kubernetes и присылай короткую сводку', error: false }) }),
        rebuild: () => this.commit({ nb: Object.assign({}, this.S.nb, { phase: 'brief', draft: null }) }),
        create: () => this.createBot(),
        onName: (e) => this.setDraftField({ name: e.target.value }),
        d: d ? Object.assign({ name: d.name, desc: d.desc }, kf(d.kind)) : Object.assign({ name: '', desc: '' }, kf('robot')),
        modelOpts: d ? seg(modelItems, d.model, (v) => this.setDraftField({ model: v })) : [],
        whereOpts: d ? seg(whereItems, d.where, (v) => this.setDraftField({ where: v })) : [],
      },
    };
  }
}
"""


def build_html():
    screens = [screen_bots(), screen_thread(), screen_approvals(), screen_handoff(), screen_routines(), screen_procedure(), screen_incident(), screen_usage(), screen_memory(), screen_settings(), screen_newbot()]
    body = f'<div style="{COL}">\n' + '\n'.join(screens) + '\n</div>\n' + tabbar() + '\n' + toast()
    root = f'<div data-theme="{{{{theme}}}}" style="position: relative; width: {W}px; height: {HGT}px; box-sizing: border-box; overflow: hidden; background: var(--bg-canvas); color: var(--fg-default); font-family: {SANS}; display: flex; flex-direction: column;">{body}</div>'
    props = json.dumps({"theme": {"editor": "enum", "options": ["light", "dark"], "default": "light", "section": "Вид"}, "$preview": {"width": W, "height": HGT}}, ensure_ascii=False).replace("'", '&#39;')
    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>Прототип Bot Hub</title>
<script src="./support.js"></script>
</head>
<body>
<x-dc>
{HELMET}
{root}
</x-dc>
<script type="text/x-dc" data-dc-script data-props='{props}'>
{SCRIPT.strip()}
</script>
</body>
</html>
"""


def update_canvas():
    path = os.path.join(OUT_DIR, 'canvas.json')
    idx = json.load(open(path))
    now = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    idx['boards'][NAME] = {"x": 0, "y": 3608, "w": W, "h": HGT, "title": "Прототип: Play", "is_interactive": True}
    idx['order'] = [NAME] + [n for n in idx['order'] if n != NAME]
    idx.setdefault('notes', {})
    idx['notes']['t4'] = {"x": 0, "y": 3308, "text": "Кликабельный прототип: один артборд, все экраны", "kind": "title1", "maxW": 1200}
    idx['notes']['n2'] = {"x": 450, "y": 3608, "w": 360, "fill": "blue",
                          "text": "Нажми Play. Боты → тред: пиши сообщение, смотри план и ответ, Стоп прерывает; каждое третье сообщение создаёт решение. Решения: разрешить или отклонить. Тап по карточке Mac: онлайн/офлайн. Рутины: запустить, пауза. Иконки в шапке «Боты»: память и настройки; кнопка «Бот»: сборка нового бота."}
    idx['notes']['s1'] = {"x": 450, "y": 3900, "w": 240, "fill": "yellow", "kind": "sticker", "text": "Состояние живёт в одном артборде и сбрасывается при перезагрузке Play"}
    json.dump(idx, open(path, 'w'), ensure_ascii=False, indent=1)
    return now


if __name__ == '__main__':
    html = build_html()
    os.makedirs(OUT_DIR, exist_ok=True)
    open(OUT, 'w').write(html)
    update_canvas()
    print(f'{NAME}: {len(html.encode())} байт, canvas.json обновлён')
