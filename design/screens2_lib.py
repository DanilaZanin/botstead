"""Кирпичи для design/screens2-gen.py (экраны этапа 2: вход, провайдеры, терминал, браузер, запись, память, лента, разрешения).

Токены, иконки, кнопки и персонажи те же, что в screens-gen.py и proto-gen.py. Отличия:
- артборд телефона 393x852 (iPhone 15 Pro) с отступом под статус-бар и Dynamic Island (54px) и под home-indicator (34px);
- у каждого артборда один переключатель «Состояние» (Tweaks): данные, пусто, загрузка, ошибка и свои варианты;
  каждое состояние лежит в своём <sc-if>, кнопки с go=... переключают состояние в Play.
Новых цветов и шрифтов нет: только токены design/system/tokens.json.
"""
import itertools
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import avatars  # noqa: E402

OUT = os.path.join(HERE, 'screens')
T = json.load(open(os.path.join(HERE, 'system', 'tokens.json'), encoding='utf-8'))
BRAND = 'botstead'
PW, PH_ = 393, 852
DW, DH = 1440, 900


def _vars(theme):
    out = []
    for fam in ('color', 'shadow'):
        for t in T[fam]['tokens']:
            v = t['value']
            out.append('--%s:%s' % (t['name'], v if isinstance(v, str) else v.get(theme, v['light'])))
    return ';'.join(out)


STATIC = ';'.join('--%s:%s' % (t['name'], t['value']) for fam in ('spacing', 'radius', 'size') for t in T[fam]['tokens'])
FONTS = '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Unbounded:wght@600&amp;family=Golos+Text:wght@400;500;600&amp;family=JetBrains+Mono:wght@400;500&amp;display=swap">'
HELMET = ('<helmet>\n' + FONTS + '\n<style>\nbody{margin:0}\n'
          '[data-theme="light"]{' + _vars('light') + ';' + STATIC + '}\n'
          '[data-theme="dark"]{' + _vars('dark') + ';' + STATIC + '}\n'
          'a{color:var(--focus)}a:hover{opacity:.85}\n'
          'sc-if,sc-for{display:contents}\n'
          'button{font-family:inherit;cursor:pointer;-webkit-tap-highlight-color:transparent}\n'
          'input,textarea{font-family:inherit}\n'
          ':focus-visible{outline:2px solid var(--focus);outline-offset:2px}\n'
          '@keyframes bh-spin{to{transform:rotate(360deg)}}\n'
          '.spin{display:inline-flex;animation:bh-spin 1s linear infinite}\n'
          '@media (prefers-reduced-motion: reduce){.spin{animation:none}}\n'
          '</style>\n</helmet>')

SANS = "'Golos Text', -apple-system, system-ui, sans-serif"
DISP = "'Unbounded', 'Golos Text', sans-serif"
MONO = "'JetBrains Mono', ui-monospace, Menlo, monospace"
# стили текста из tokens.json (type)
LARGE = f'font: 600 30px/36px {DISP}; letter-spacing: -0.02em;'
TITLE = f'font: 600 20px/26px {DISP}; letter-spacing: -0.01em;'
HEAD = f'font: 600 17px/22px {SANS};'
BODY = f'font: 400 17px/24px {SANS};'
CALL = f'font: 400 15px/20px {SANS};'
CALLB = f'font: 600 15px/20px {SANS};'
FN = f'font: 400 13px/18px {SANS}; color: var(--fg-muted);'
CAP = f'font: 600 12px/16px {SANS}; letter-spacing: 0.02em;'
LOG = f'font: 400 13px/18px {MONO};'
CARD = 'background: var(--bg-surface); border: 1px solid var(--border-default); border-radius: 16px;'
ELL = 'white-space: nowrap; overflow: hidden; text-overflow: ellipsis;'
SRONLY = 'position: absolute; width: 1px; height: 1px; overflow: hidden;'

IC = {
    'plus': '<path d="M12 5v14M5 12h14"></path>', 'send': '<path d="M12 19V5M5 12l7-7 7 7"></path>',
    'stop': '<rect x="7" y="7" width="10" height="10" rx="2"></rect>', 'screen': '<rect x="3" y="4" width="18" height="12" rx="2"></rect><path d="M8 20h8M12 16v4"></path>',
    'risk': '<path d="M12 3l9 16H3z"></path><path d="M12 10v4M12 17h.01"></path>', 'check': '<path d="M5 12l5 5 9-10"></path>',
    'x': '<path d="M6 6l12 12M18 6L6 18"></path>', 'chev': '<path d="M9 6l6 6-6 6"></path>', 'back': '<path d="M15 6l-6 6 6 6"></path>',
    'down': '<path d="M6 9l6 6 6-6"></path>', 'bot': '<rect x="4" y="7" width="16" height="12" rx="3"></rect><path d="M12 3v4M9 13h.01M15 13h.01"></path>',
    'clock': '<circle cx="12" cy="12" r="9"></circle><path d="M12 7v5l3 2"></path>',
    'shield': '<path d="M12 3l8 4v5c0 5-3.5 8-8 9-4.5-1-8-4-8-9V7z"></path><path d="M9 12l2 2 4-4"></path>',
    'bars': '<path d="M5 20V10M12 20V4M19 20v-7"></path>', 'play': '<path d="M8 5l11 7-11 7z"></path>',
    'laptop': '<rect x="4" y="5" width="16" height="11" rx="2"></rect><path d="M2 19h20"></path>',
    'search': '<circle cx="11" cy="11" r="7"></circle><path d="M20 20l-4-4"></path>',
    'hand': '<path d="M8 13V5a1.5 1.5 0 013 0v6M11 11V4a1.5 1.5 0 013 0v7M14 11V5.5a1.5 1.5 0 013 0V14c0 4-2.5 7-6.5 7S5 18 4.5 15L3 11.5a1.5 1.5 0 012.6-1.4L8 13"></path>',
    'bolt': '<path d="M13 2L4 14h7l-1 8 9-12h-7z"></path>', 'folder': '<path d="M3 6h6l2 2h10v11H3z"></path>',
    'brain': '<path d="M12 4a4 4 0 00-4 4 4 4 0 00-2 7 4 4 0 006 4 4 4 0 006-4 4 4 0 00-2-7 4 4 0 00-4-4z"></path><path d="M12 4v16"></path>',
    'gear': '<circle cx="12" cy="12" r="3"></circle><path d="M12 2v3M12 19v3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1M2 12h3M19 12h3M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1"></path>',
    'list': '<path d="M9 6h11M9 12h11M9 18h11M4 6h.01M4 12h.01M4 18h.01"></path>', 'eye': '<path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12z"></path><circle cx="12" cy="12" r="3"></circle>',
    'spin': '<path d="M12 3a9 9 0 109 9"></path>',
    # новые иконки этапа 2: тот же стиль, 24x24, линия 2, скруглённые концы
    'key': '<circle cx="8" cy="15" r="4"></circle><path d="M11 12l9-9M16 7l3 3"></path>',
    'link': '<path d="M10 14a4 4 0 005.7 0l3-3a4 4 0 00-5.7-5.7l-1 1"></path><path d="M14 10a4 4 0 00-5.7 0l-3 3a4 4 0 005.7 5.7l1-1"></path>',
    'copy': '<rect x="9" y="9" width="11" height="11" rx="2"></rect><path d="M5 15V6a2 2 0 012-2h9"></path>',
    'user': '<circle cx="12" cy="8" r="4"></circle><path d="M4 21a8 8 0 0116 0"></path>',
    'lock': '<rect x="5" y="11" width="14" height="10" rx="2"></rect><path d="M8 11V7a4 4 0 018 0v4"></path>',
    'globe': '<circle cx="12" cy="12" r="9"></circle><path d="M3 12h18M12 3a14 14 0 010 18M12 3a14 14 0 000 18"></path>',
    'term': '<rect x="3" y="4" width="18" height="16" rx="2"></rect><path d="M7 9l3 3-3 3M13 15h4"></path>',
    'rec': '<circle cx="12" cy="12" r="9"></circle><circle cx="12" cy="12" r="3.5" fill="currentColor"></circle>',
    'pause': '<path d="M8 5v14M16 5v14"></path>', 'refresh': '<path d="M20 11a8 8 0 00-14.5-4M4 4v4h4"></path><path d="M4 13a8 8 0 0014.5 4M20 20v-4h-4"></path>',
    'trash': '<path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"></path>', 'edit': '<path d="M4 20h4L19 9l-4-4L4 16z"></path><path d="M13 7l4 4"></path>',
    'more': '<path d="M5 12h.01M12 12h.01M19 12h.01"></path>', 'info': '<circle cx="12" cy="12" r="9"></circle><path d="M12 11v6M12 7.5h.01"></path>',
    'plug': '<path d="M9 3v5M15 3v5M6 8h12v3a6 6 0 01-12 0zM12 17v4"></path>', 'ext': '<path d="M14 4h6v6M20 4l-9 9"></path><path d="M18 14v5a1 1 0 01-1 1H5a1 1 0 01-1-1V7a1 1 0 011-1h5"></path>',
    'kbd': '<rect x="2" y="6" width="20" height="12" rx="2"></rect><path d="M6 10h.01M10 10h.01M14 10h.01M18 10h.01M7 14h10"></path>',
    'cursor': '<path d="M5 3l14 8-6 2-2 6z"></path>', 'type': '<path d="M5 6V4h14v2M12 4v16M9 20h6"></path>',
    'ban': '<circle cx="12" cy="12" r="9"></circle><path d="M5.6 5.6l12.8 12.8"></path>', 'chat': '<path d="M4 5h16v11H9l-5 4z"></path>',
    'cloudoff': '<path d="M3 3l18 18"></path><path d="M7 7a5 5 0 00-1 9.9h10M10 5.2A6 6 0 0118 10a4 4 0 012.6 6.5"></path>',
    'sliders': '<path d="M4 6h9M19 6h1M4 12h3M13 12h7M4 18h11"></path><circle cx="16" cy="6" r="2.5"></circle><circle cx="10" cy="12" r="2.5"></circle><circle cx="18" cy="18" r="2.5"></circle>',
    'eyeoff': '<path d="M3 3l18 18"></path><path d="M10.6 5.1A10 10 0 0122 12a15 15 0 01-3 3.6M6.5 6.6A15 15 0 002 12s4 7 10 7a10 10 0 004.3-1"></path>',
    'expand': '<path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"></path>',
    'zoom': '<circle cx="11" cy="11" r="7"></circle><path d="M20 20l-4-4M11 8v6M8 11h6"></path>',
    'paste': '<rect x="6" y="5" width="12" height="16" rx="2"></rect><path d="M9 5V3h6v2M9 12h6M9 16h4"></path>',
}


def ic(n, s=20, w=2):
    return f'<svg width="{s}" height="{s}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="{w}" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" style="flex-shrink: 0;">{IC[n]}</svg>'


def H(p):
    return '{{' + p + '}}'


_id = itertools.count(1)


def uid(p='f'):
    return f'{p}{next(_id)}'


def spin(s=16):
    return f'<span class="spin">{ic("spin", s, 2.4)}</span>'


def _el(style, inner, href=None, go=None, label=None, extra=''):
    al = f' aria-label="{label}"' if label else ''
    if href:
        return f'<a href="{href}"{al}{extra} style="{style} text-decoration: none;">{inner}</a>'
    oc = f' onClick="{H("go_" + go)}"' if go else ''
    return f'<button type="button"{al}{oc}{extra} style="{style}">{inner}</button>'


BTN = {'primary': 'background: var(--bg-emphasis); color: var(--fg-on-emphasis); border: 1px solid transparent;',
       'secondary': 'background: var(--bg-surface); color: var(--fg-default); border: 1px solid var(--border-control);',
       'ghost': 'background: transparent; color: var(--fg-default); border: 1px solid transparent;',
       'approve': 'background: var(--attention-emphasis); color: var(--fg-on-attention); border: 1px solid transparent;',
       'danger': 'background: var(--danger-bg); color: var(--danger-fg); border: 1px solid transparent;'}


def btn(kind, text, icon=None, href=None, go=None, extra='', disabled=False, busy=False, label=None):
    inner = (spin() if busy else (ic(icon, 16, 2.4) if icon else '')) + text
    s = f'min-height: 44px; padding: 0 16px; border-radius: 12px; {BTN[kind]} font: {600 if kind == 'primary' else 500} 15px/20px {SANS}; display: inline-flex; align-items: center; justify-content: center; gap: 8px; box-sizing: border-box; white-space: nowrap; {"opacity: 0.45; " if disabled else ""}{extra}'
    at = (' disabled' if disabled else '') + (' aria-busy="true"' if busy else '')
    return _el(s, inner, href, go, label, at)


def iconbtn(icon, label, kind='secondary', href=None, go=None, round_=False):
    st = {'primary': 'background: var(--bg-emphasis); color: var(--fg-on-emphasis); border: none;', 'secondary': 'background: var(--bg-sunken); color: var(--fg-default); border: none;',
          'ghost': 'background: transparent; color: var(--fg-default); border: none;', 'danger': 'background: var(--danger-bg); color: var(--danger-fg); border: none;',
          'muted': 'background: transparent; color: var(--fg-muted); border: none;'}[kind]
    s = f'width: 44px; height: 44px; padding: 0; flex-shrink: 0; border-radius: {22 if round_ else 12}px; {st} display: flex; align-items: center; justify-content: center;'
    return _el(s, ic(icon), href, go, label)


def badge(p, text):
    c = f'background: var(--{p}-bg); color: var(--{p}-fg);' if p in ('claude', 'codex', 'gemini') else 'background: var(--bg-sunken); color: var(--fg-muted);'
    return f'<span style="display: inline-flex; align-items: center; padding: 2px 7px; border-radius: 6px; {c} {CAP} white-space: nowrap;">{text}</span>'


def tag(text, kind='neutral', icon=None):
    c = {'neutral': 'background: var(--bg-sunken); color: var(--fg-muted);', 'attention': 'background: var(--attention-bg); color: var(--attention-text);',
         'danger': 'background: var(--danger-bg); color: var(--danger-fg);', 'success': 'background: var(--bg-sunken); color: var(--success-fg);'}[kind]
    return f'<span style="display: inline-flex; align-items: center; gap: 4px; padding: 2px 7px; border-radius: 6px; {c} {CAP} white-space: nowrap;">{ic(icon, 12, 2.4) if icon else ""}{text}</span>'


KIND = {'С': 'scout', 'М': 'mac', 'S': 'sre', 'К': 'coder', 'А': 'archive'}


def avatar(p, letter, size=44):
    return avatars.avatar_html(KIND.get(letter, 'robot'), p, size)


def status(c, text, icon=None):
    """StatusBadge: точка 8px (или иконка у ошибки и паузы) плюс слово."""
    mark = f'<span style="color: var(--{c}); display: flex;">{ic(icon, 14, 2.6)}</span>' if icon else f'<span style="width: 8px; height: 8px; border-radius: 4px; background: var(--{c}); flex-shrink: 0;"></span>'
    return f'<span style="display: inline-flex; align-items: center; gap: 6px; {FN} color: var(--fg-default);">{mark}{text}</span>'


def section(t, right=''):
    return f'<div style="display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-top: 6px; flex-shrink: 0;"><h2 style="margin: 0; font: 600 12px/16px {SANS}; letter-spacing: 0.04em; text-transform: uppercase; color: var(--fg-muted);">{t}</h2>{right}</div>'


def card(inner, pad='14px', gap=10, extra=''):
    return f'<div style="{CARD} padding: {pad}; display: flex; flex-direction: column; gap: {gap}px; flex-shrink: 0; {extra}">{inner}</div>'


def rows(items, pad=10):
    """Сгруппированный список: карточка со строками, строка не ниже 44px."""
    out = ''
    for i, it in enumerate(items):
        out += f'<div style="display: flex; align-items: center; gap: 12px; min-height: 44px; padding: {pad}px 14px; box-sizing: border-box; {"border-top: 1px solid var(--border-default);" if i else ""}">{it}</div>'
    return f'<div style="{CARD} display: flex; flex-direction: column; flex-shrink: 0; overflow: hidden;">{out}</div>'


def two(title, sub='', extra='', mono=False):
    s = f'<span style="{LOG if mono else FN} color: var(--fg-muted); {ELL}">{sub}</span>' if sub else ''
    return f'<span style="flex-grow: 1; min-width: 0; display: flex; flex-direction: column; gap: 2px;"><span style="{CALLB} color: var(--fg-default);">{title}</span>{s}{extra}</span>'


def tile(icon, tone='neutral'):
    c = {'neutral': 'background: var(--bg-sunken); color: var(--fg-default);', 'attention': 'background: var(--attention-bg); color: var(--attention-text);',
         'danger': 'background: var(--danger-bg); color: var(--danger-fg);', 'claude': 'background: var(--claude-bg); color: var(--claude-fg);',
         'codex': 'background: var(--codex-bg); color: var(--codex-fg);', 'gemini': 'background: var(--gemini-bg); color: var(--gemini-fg);'}[tone]
    return f'<span aria-hidden="true" style="width: 40px; height: 40px; flex-shrink: 0; border-radius: 12px; {c} display: flex; align-items: center; justify-content: center;">{ic(icon, 20)}</span>'


def field(label, value='', ph='', typ='text', hint='', err='', mono=False, right='', disabled=False):
    i = uid()
    border = 'var(--danger-fg)' if err else 'var(--border-control)'
    note = f'<span id="{i}n" style="display: flex; align-items: center; gap: 6px; {FN} color: var(--danger-fg);">{ic("risk", 14, 2.4)}{err}</span>' if err else (f'<span id="{i}n" style="{FN}">{hint}</span>' if hint else '')
    desc = f' aria-describedby="{i}n"' if note else ''
    inv = ' aria-invalid="true"' if err else ''
    inp = f'<input id="{i}" type="{typ}" value="{value}" placeholder="{ph}"{desc}{inv}{" disabled" if disabled else ""} style="flex-grow: 1; min-width: 0; height: 44px; box-sizing: border-box; padding: 0 14px; border-radius: 12px; border: {"2px" if err else "1px"} solid {border}; background: var(--bg-surface); color: var(--fg-default); font: 400 15px/20px {MONO if mono else SANS}; {"opacity: 0.6;" if disabled else ""}">'
    return f'<div style="display: flex; flex-direction: column; gap: 6px; flex-shrink: 0;"><label for="{i}" style="{FN}">{label}</label><div style="display: flex; gap: 8px; align-items: center;">{inp}{right}</div>{note}</div>'


def switch(on, label, sub='', disabled=False):
    """Switch: строка-переключатель, вся строка 44px, состояние видно положением ручки и словом aria-checked."""
    i = uid('s')
    track = 'background: var(--bg-emphasis); border: 1px solid transparent; justify-content: flex-end;' if on else 'background: var(--bg-surface); border: 1px solid var(--border-control); justify-content: flex-start;'
    knob = 'background: var(--fg-on-emphasis);' if on else 'background: var(--fg-muted);'
    s = f'<span style="{FN}">{sub}</span>' if sub else ''
    return (f'<div style="display: flex; align-items: center; gap: 12px; min-height: 44px; flex-grow: 1; min-width: 0; {"opacity: 0.5;" if disabled else ""}"><span id="{i}" style="flex-grow: 1; min-width: 0; display: flex; flex-direction: column; gap: 2px;"><span style="{CALL} color: var(--fg-default);">{label}</span>{s}</span>'
            f'<button type="button" role="switch" aria-checked="{"true" if on else "false"}" aria-labelledby="{i}"{" disabled" if disabled else ""} style="width: 52px; height: 44px; padding: 6px 0; flex-shrink: 0; border: none; background: transparent; display: flex; align-items: center;"><span style="width: 52px; height: 32px; box-sizing: border-box; border-radius: 16px; padding: 3px; display: flex; align-items: center; {track}"><span style="width: 24px; height: 24px; border-radius: 12px; {knob}"></span></span></button></div>')


def seg(opts, active, label='', gos=None):
    """SegmentedControl: 2-4 взаимоисключающих варианта."""
    cells = ''
    for i, o in enumerate(opts):
        oc = f' onClick="{H("go_" + gos[i])}"' if gos and gos[i] else ''
        cells += f'<button type="button" role="radio" aria-checked="{"true" if i == active else "false"}"{oc} style="min-height: 44px; padding: 0 6px; border-radius: 9px; border: {"1px solid var(--border-default)" if i == active else "1px solid transparent"}; background: {"var(--bg-surface)" if i == active else "transparent"}; color: var(--fg-default); font: {"600" if i == active else "500"} 14px/18px {SANS};">{o}</button>'
    return f'<div role="radiogroup" aria-label="{label}" style="display: grid; grid-template-columns: repeat({len(opts)}, minmax(0, 1fr)); gap: 4px; padding: 4px; border-radius: 12px; background: var(--bg-sunken); flex-shrink: 0;">{cells}</div>'


def radio(on, title, sub='', right='', disabled=False, go=None, icon=''):
    mark = f'<span aria-hidden="true" style="width: 22px; height: 22px; flex-shrink: 0; box-sizing: border-box; border-radius: 11px; border: 2px solid var(--{"fg-default" if on else "border-control"}); display: flex; align-items: center; justify-content: center;">{"<span style=\"width: 10px; height: 10px; border-radius: 5px; background: var(--fg-default);\"></span>" if on else ""}</span>'
    oc = f' onClick="{H("go_" + go)}"' if go else ''
    s = f'<span style="{FN}">{sub}</span>' if sub else ''
    return f'<button type="button" role="radio" aria-checked="{"true" if on else "false"}"{oc}{" disabled" if disabled else ""} style="display: flex; align-items: center; gap: 12px; min-height: 44px; width: 100%; padding: 0; border: none; background: transparent; text-align: left; {"opacity: 0.5;" if disabled else ""}">{mark}{icon}<span style="flex-grow: 1; min-width: 0; display: flex; flex-direction: column; gap: 2px;"><span style="{CALLB if on else CALL} color: var(--fg-default);">{title}</span>{s}</span>{right}</button>'


def chips(items, active=0):
    out = ''.join(f'<button type="button" aria-pressed="{"true" if i == active else "false"}" style="min-height: 44px; padding: 0 14px; flex-shrink: 0; border-radius: 22px; border: 1px solid {"transparent" if i == active else "var(--border-control)"}; background: {"var(--bg-emphasis)" if i == active else "var(--bg-surface)"}; color: var(--{"fg-on-emphasis" if i == active else "fg-default"}); font: 500 14px/18px {SANS}; display: inline-flex; align-items: center; gap: 6px; white-space: nowrap;">{t}</button>' for i, t in enumerate(items))
    return f'<div role="group" aria-label="Фильтр" style="display: flex; gap: 8px; overflow: hidden; flex-shrink: 0;">{out}</div>'


def banner(kind, icon, text, sub='', action=''):
    """Banner: строка состояния экрана. attention: нужен человек, danger: сбой, neutral: справка, success: готово."""
    c = {'attention': 'background: var(--attention-bg); border: 1px solid var(--attention-border); color: var(--attention-text);',
         'danger': 'background: var(--danger-bg); border: 1px solid transparent; color: var(--danger-fg);',
         'neutral': 'background: var(--bg-sunken); border: 1px solid var(--border-default); color: var(--fg-default);',
         'success': 'background: var(--bg-surface); border: 1px solid var(--border-default); color: var(--fg-default);'}[kind]
    icol = ' color: var(--success-fg);' if kind == 'success' else ''
    s = f'<span style="font: 400 13px/18px {SANS}; color: var(--fg-default);">{sub}</span>' if sub else ''
    i = spin(18) if icon == 'spin' else ic(icon, 20)
    return f'<div role="status" style="display: flex; align-items: center; gap: 10px; padding: 10px 14px; border-radius: 14px; {c} flex-shrink: 0;"><span style="display: flex;{icol}">{i}</span><span style="flex-grow: 1; min-width: 0; display: flex; flex-direction: column; gap: 2px;"><span style="{CALLB}">{text}</span>{s}</span>{action}</div>'


def meter(name, p, c, right=None, wide=76):
    r = right if right is not None else f'{p}%'
    return f'<div style="display: flex; align-items: center; gap: 10px; font: 400 13px/18px {SANS};"><span style="width: {wide}px; flex-shrink: 0; font-weight: 600; color: var(--fg-default);">{name}</span><div role="meter" aria-label="{name}: {p}%" aria-valuenow="{p}" aria-valuemin="0" aria-valuemax="100" style="flex-grow: 1; height: 8px; border-radius: 4px; background: var(--bg-sunken); overflow: hidden;"><div style="width: {min(p, 100)}%; height: 8px; border-radius: 4px; background: var(--{c});"></div></div><span style="flex-shrink: 0; text-align: right; font: 400 12px/16px {MONO}; color: var(--{"danger-fg" if p >= 100 else "fg-muted"});">{r}</span></div>'


# ---- состояния экрана (ScreenState) ----
def empty(icon, title, text, action=''):
    return f'<div style="flex-grow: 1; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 10px; padding: 24px; text-align: center;"><span aria-hidden="true" style="width: 56px; height: 56px; border-radius: 28px; background: var(--bg-sunken); color: var(--fg-muted); display: flex; align-items: center; justify-content: center;">{ic(icon, 26)}</span><span style="{HEAD} color: var(--fg-default);">{title}</span><span style="{FN} max-width: 300px;">{text}</span><div style="display: flex; flex-direction: column; gap: 8px; margin-top: 6px;">{action}</div></div>'


def error(title, text, action=''):
    return f'<div role="alert" style="flex-grow: 1; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 10px; padding: 24px; text-align: center;"><span aria-hidden="true" style="width: 56px; height: 56px; border-radius: 28px; background: var(--danger-bg); color: var(--danger-fg); display: flex; align-items: center; justify-content: center;">{ic("risk", 26)}</span><span style="{HEAD} color: var(--fg-default);">{title}</span><span style="{FN} max-width: 300px;">{text}</span><div style="display: flex; flex-direction: column; gap: 8px; margin-top: 6px;">{action}</div></div>'


def loading(text, n=4, h=64):
    sk = ''.join(f'<div style="{CARD} height: {h}px; padding: 14px; box-sizing: border-box; display: flex; flex-direction: column; gap: 8px; justify-content: center; flex-shrink: 0;"><span style="width: {55 - i * 7}%; height: 12px; border-radius: 6px; background: var(--bg-sunken);"></span><span style="width: {80 - i * 9}%; height: 10px; border-radius: 5px; background: var(--bg-sunken);"></span></div>' for i in range(n))
    return f'<div role="status" aria-busy="true" style="display: flex; flex-direction: column; gap: 10px;"><span style="display: flex; align-items: center; gap: 8px; {FN}">{spin(14)}{text}</span>{sk}</div>'


# ---- каркас телефона ----
def sbar():
    return f'<div aria-hidden="true" style="height: 54px; flex-shrink: 0; box-sizing: border-box; padding: 19px 0 0 50px; font: 600 16px/20px {SANS}; color: var(--fg-default);">9:41</div>'


def hdr_root(title, action=''):
    return f'<header style="flex-shrink: 0;">{sbar()}<div style="padding: 6px 16px 12px; display: flex; align-items: center; justify-content: space-between; gap: 12px;"><h1 style="margin: 0; {LARGE} color: var(--fg-default);">{title}</h1>{action}</div></header>'


def hdr_back(title, sub='', href=None, go=None, av='', right='', back_icon='back', back_label='Назад'):
    s = f'<span style="{FN} {ELL}">{sub}</span>' if sub else ''
    return f'<header style="flex-shrink: 0; background: var(--bg-glass); border-bottom: 1px solid var(--border-default); backdrop-filter: blur(20px);">{sbar()}<div style="padding: 2px 12px 10px; display: flex; align-items: center; gap: 8px;">{iconbtn(back_icon, back_label, "ghost", href, go)}{av}<div style="flex-grow: 1; min-width: 0; display: flex; flex-direction: column;"><h1 style="margin: 0; {HEAD} color: var(--fg-default); {ELL}">{title}</h1>{s}</div>{right}</div></header>'


def scroll(inner, extra=''):
    return f'<div style="flex-grow: 1; min-height: 0; overflow: hidden; padding: 16px; display: flex; flex-direction: column; gap: 12px; {extra}">{inner}</div>'


def bottom(inner, cols=None):
    lay = f'display: grid; grid-template-columns: {cols}; gap: 8px;' if cols else 'display: flex; flex-direction: column; gap: 8px;'
    return f'<div style="flex-shrink: 0; padding: 10px 16px 34px; background: var(--bg-surface); border-top: 1px solid var(--border-default); {lay}">{inner}</div>'


def actionbar(inner):
    """Главное действие экрана с таб-баром: в зоне большого пальца, прямо над вкладками."""
    return f'<div style="flex-shrink: 0; padding: 10px 16px; background: var(--bg-surface); border-top: 1px solid var(--border-default); display: flex; flex-direction: column;">{inner}</div>'


TABS = [('bot', 'Боты', 'Main.dc.html'), ('clock', 'Рутины', 'Routines.dc.html'), ('shield', 'Решения', 'Approval.dc.html'), ('bars', 'Расход', 'Usage.dc.html')]


def tabbar(active):
    items = ''
    for i, (icn, t, h) in enumerate(TABS):
        a = i == active
        d = '<span aria-label="есть ожидающие" style="position: absolute; top: 4px; right: 30%; width: 8px; height: 8px; border-radius: 4px; background: var(--attention-fg);"></span>' if t == 'Решения' else ''
        cur = ' aria-current="page"' if a else ''
        items += f'<a href="{h}"{cur} style="position: relative; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 3px; min-height: 48px; text-decoration: none; color: var(--{"fg-default" if a else "fg-muted"}); font: 600 11px/14px {SANS};">{ic(icn, 22)}{t}{d}</a>'
    return f'<nav aria-label="Разделы" style="flex-shrink: 0; display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); padding: 6px 8px 30px; background: var(--bg-glass); border-top: 1px solid var(--border-default); backdrop-filter: blur(20px);">{items}</nav>'


def sheet(title, inner, sub='', av='', close=None, max_h=None):
    """Sheet: нижний лист поверх экрана. Затемнение, ручка, заголовок title, тень shadow-sheet."""
    i = uid('sh')
    x = iconbtn('x', 'Закрыть', 'ghost', None, close) if close else ''
    s = f'<span style="{FN}">{sub}</span>' if sub else ''
    return (f'<div style="position: absolute; left: 0; top: 0; width: 100%; height: 100%; display: flex; flex-direction: column; justify-content: flex-end; background: rgba(0, 0, 0, 0.35);">'
            f'<section role="dialog" aria-modal="true" aria-labelledby="{i}" style="background: var(--bg-surface); border-radius: 16px 16px 0 0; box-shadow: var(--shadow-sheet); padding: 8px 16px 34px; display: flex; flex-direction: column; gap: 12px; {f"max-height: {max_h}px; overflow: hidden;" if max_h else ""}">'
            f'<div aria-hidden="true" style="align-self: center; width: 36px; height: 5px; border-radius: 3px; background: var(--border-control); flex-shrink: 0;"></div>'
            f'<div style="display: flex; align-items: center; gap: 10px; flex-shrink: 0;">{av}<div style="flex-grow: 1; min-width: 0; display: flex; flex-direction: column;"><h2 id="{i}" style="margin: 0; {TITLE} color: var(--fg-default);">{title}</h2>{s}</div>{x}</div>{inner}</section></div>')


def dialog(title, inner, sub='', close=None, w=480):
    """Диалог на Mac: тот же Sheet, но по центру окна."""
    i = uid('dg')
    x = iconbtn('x', 'Закрыть', 'ghost', None, close) if close else ''
    s = f'<span style="{FN}">{sub}</span>' if sub else ''
    return (f'<div style="position: absolute; left: 0; top: 0; width: 100%; height: 100%; display: flex; align-items: center; justify-content: center; background: rgba(0, 0, 0, 0.35);">'
            f'<section role="dialog" aria-modal="true" aria-labelledby="{i}" style="width: {w}px; background: var(--bg-surface); border: 1px solid var(--border-default); border-radius: 16px; box-shadow: var(--shadow-sheet); padding: 20px; display: flex; flex-direction: column; gap: 14px;">'
            f'<div style="display: flex; align-items: center; gap: 10px;"><div style="flex-grow: 1; display: flex; flex-direction: column;"><h2 id="{i}" style="margin: 0; {TITLE} color: var(--fg-default);">{title}</h2>{s}</div>{x}</div>{inner}</section></div>')


# ---- терминал ----
def term(lines, h=None, fs=12, label='Терминал входа', cursor=True, grow=False):
    """Terminal: lines = [(текст, тон)], тон: '' обычный, 'm' приглушённый, 'ok', 'err', 'warn', 'link'."""
    tone = {'': 'var(--fg-default)', 'm': 'var(--fg-muted)', 'ok': 'var(--success-fg)', 'err': 'var(--danger-fg)', 'warn': 'var(--attention-fg)', 'link': 'var(--focus)'}
    out = ''.join(f'<div style="color: {tone[c]};{" text-decoration: underline;" if c == "link" else ""}">{t if t else "&nbsp;"}</div>' for t, c in lines)
    cur = f'<span aria-hidden="true" style="display: inline-block; width: {round(fs * 0.6)}px; height: {fs + 3}px; background: var(--fg-default); vertical-align: text-bottom;"></span>' if cursor else ''
    size = (f'height: {h}px; ' if h else '') + ('flex-grow: 1; min-height: 0; ' if grow else 'flex-shrink: 0; ')
    return f'<div role="log" aria-label="{label}" tabindex="0" style="{size}box-sizing: border-box; padding: 10px 12px; border-radius: 12px; border: 1px solid var(--border-control); background: var(--bg-surface); font: 400 {fs}px/{round(fs * 1.5)}px {MONO}; white-space: pre-wrap; overflow-wrap: anywhere; overflow: hidden; display: flex; flex-direction: column; justify-content: flex-end;">{out}{f"<div>{cur}</div>" if cursor else ""}</div>'


def keybar(keys):
    """Клавиши над экранной клавиатурой: каждая 44px высотой."""
    out = ''
    for k, a in keys:
        al = f' aria-label="{a}"' if a else ''
        out += f'<button type="button"{al} style="min-width: 44px; height: 44px; padding: 0 10px; flex-shrink: 0; border-radius: 10px; border: 1px solid var(--border-control); background: var(--bg-surface); color: var(--fg-default); font: 500 13px/18px {MONO}; display: inline-flex; align-items: center; justify-content: center; gap: 6px;">{k}</button>'
    return f'<div role="toolbar" aria-label="Клавиши терминала" style="flex-shrink: 0; display: flex; gap: 6px; padding: 6px 8px; background: var(--bg-surface); border-top: 1px solid var(--border-default); overflow: hidden;">{out}</div>'


def oskbd(h=291):
    return f'<div aria-hidden="true" style="height: {h}px; flex-shrink: 0; background: var(--bg-sunken); border-top: 1px solid var(--border-default); display: flex; align-items: center; justify-content: center; {FN}">Клавиатура iOS</div>'


# ---- экран браузера бота ----
def live(w, h, inner='', frame='border-default', note='Живой экран бота', tools=False):
    """Живой экран (поток noVNC) с условной страницей внутри."""
    pg = inner or (f'<div style="width: 62%; display: flex; flex-direction: column; gap: {max(6, h // 28)}px;"><span style="width: 70%; height: {max(8, h // 22)}px; border-radius: 4px; background: var(--border-control);"></span><span style="height: 6px; border-radius: 3px; background: var(--border-default);"></span><span style="width: 88%; height: 6px; border-radius: 3px; background: var(--border-default);"></span>'
                   f'<span style="height: {max(18, h // 10)}px; border-radius: 6px; border: 1px solid var(--border-control); background: var(--bg-surface);"></span><span style="height: {max(18, h // 10)}px; border-radius: 6px; border: 2px solid var(--focus); background: var(--bg-surface);"></span><span style="width: 40%; height: {max(18, h // 10)}px; border-radius: 6px; background: var(--border-control);"></span></div>')
    tb = 'width: 44px; height: 44px; padding: 0; border-radius: 12px; border: 1px solid var(--border-control); background: var(--bg-surface); color: var(--fg-default); display: flex; align-items: center; justify-content: center;'
    tl = f'<div style="position: absolute; right: 8px; bottom: 8px; display: flex; gap: 8px;"><button type="button" aria-label="Масштаб экрана" style="{tb}">{ic("zoom", 20)}</button><button type="button" aria-label="На весь экран" style="{tb}">{ic("expand", 20)}</button></div>' if tools else ''
    return f'<div role="group" aria-label="{note}" style="position: relative; width: {w}; height: {h}px; flex-shrink: 0; box-sizing: border-box; border: 2px solid var(--{frame}); background: var(--bg-canvas); display: flex; align-items: center; justify-content: center; overflow: hidden;">{pg}{tl}</div>'


def addr(url, who='bot', secure=True):
    """Адрес страницы: всегда только для чтения. who='human': управляет человек, рамка attention и подпись «управляете вы»."""
    human = who == 'human'
    lab = (f'<span style="display: inline-flex; align-items: center; gap: 4px; {CAP} color: var(--attention-text); flex-shrink: 0;">{ic("hand", 12, 2.4)}управляете вы</span>' if human else f'<span style="{CAP} flex-shrink: 0;">только чтение</span>')
    return f'<div role="group" aria-label="Адрес страницы, только чтение" style="display: flex; align-items: center; gap: 8px; min-height: 36px; padding: 0 12px; border-radius: 10px; background: var(--{"attention-bg" if human else "bg-sunken"}); border: 1px solid var(--{"attention-border" if human else "border-default"}); color: var(--fg-muted); flex-grow: 1; min-width: 0;">{ic("lock" if secure else "globe", 14, 2.4)}<span style="flex-grow: 1; min-width: 0; font: 400 13px/18px {MONO}; color: var(--fg-default); {ELL}">{url}</span>{lab}</div>'


STEP = {'done': ('check', 'success-fg', 'готово'), 'run': ('spin', 'fg-default', 'идёт'), 'fail': ('x', 'danger-fg', 'сбой'), 'wait': ('shield', 'attention-fg', 'ждёт решения'), 'todo': (None, 'fg-muted', 'в очереди'), 'pause': ('pause', 'fg-muted', 'на паузе'), 'skip': ('ban', 'fg-muted', 'пропущен')}


def step(n, st, title, detail='', extra='', tone=None):
    icn, col, word = STEP[st]
    mark = spin(14) if icn == 'spin' else (ic(icn, 14, 2.6) if icn else f'<span style="font: 600 12px/16px {MONO};">{n}</span>')
    d = f'<span style="font: 400 12px/16px {MONO}; color: var(--fg-muted); overflow-wrap: anywhere;">{detail}</span>' if detail else ''
    bg = {'fail': 'background: var(--danger-bg); border-radius: 12px; padding: 10px;', 'wait': 'background: var(--attention-bg); border: 1px solid var(--attention-border); border-radius: 12px; padding: 10px;'}.get(tone or '', '')
    return f'<li aria-label="Шаг {n}: {word}" style="display: flex; gap: 10px; align-items: flex-start; {bg}"><span style="width: 24px; height: 24px; flex-shrink: 0; border-radius: 12px; background: var(--bg-sunken); color: var(--{"attention-text" if tone == "wait" else col}); display: flex; align-items: center; justify-content: center;">{mark}</span><span style="flex-grow: 1; min-width: 0; display: flex; flex-direction: column; gap: 3px; padding-top: 2px;"><span style="{CALL} color: var(--{"fg-muted" if st in ("todo", "skip") else "fg-default"});">{title}</span>{d}{extra}</span></li>'


def steps(items, gap=10):
    return f'<ol style="list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: {gap}px;">{"".join(items)}</ol>'


# ---- режимы разрешений (PermissionMode) ----
MODES = [('auto', 'Без вопроса', 'check', 'success-fg', 'Бот делает сам. Действие видно в ленте.'),
         ('ask', 'Спросить', 'shield', 'attention-fg', 'Перед каждым действием придёт запрос.'),
         ('cmd', 'По команде', 'chat', 'fg-muted', 'Только если попросить об этом прямо в сообщении.'),
         ('deny', 'Запрещено', 'ban', 'danger-fg', 'Инструмент выключен. Бот скажет, что не может.')]
MODE = {m[0]: m for m in MODES}


def mode_pill(m, go=None, locked=False):
    _, word, icn, col, _ = MODE[m]
    inner = f'<span style="color: var(--{col}); display: flex;">{ic("lock" if locked else icn, 14, 2.6)}</span>{word}' + ('' if locked else ic('down', 12, 2.6))
    s = f'min-height: 44px; padding: 0 10px; flex-shrink: 0; border-radius: 12px; border: 1px solid var(--{"border-default" if locked else "border-control"}); background: var(--bg-surface); color: var(--fg-default); font: 500 13px/18px {SANS}; display: inline-flex; align-items: center; gap: 6px; white-space: nowrap;'
    return _el(s, inner, None, go, None, ' aria-haspopup="dialog"')


# ---- персонажи: 8 готовых плюс 7 плейсхолдеров в том же стиле ----
_PH = [('circle', {'cx': '24', 'cy': '27', 'r': '13'}, 'robot', 'peach'), ('rect', {'x': '11', 'y': '13', 'width': '26', 'height': '25', 'rx': '11'}, 'scout', 'mint'),
       ('ellipse', {'cx': '24', 'cy': '28', 'rx': '14', 'ry': '11'}, 'coder', 'sand'), ('rect', {'x': '10', 'y': '12', 'width': '28', 'height': '26', 'rx': '5'}, 'spark', 'lilac'),
       ('circle', {'cx': '24', 'cy': '26', 'r': '12'}, 'sre', 'lime'), ('ellipse', {'cx': '24', 'cy': '26', 'rx': '11', 'ry': '14'}, 'mac', 'sand'),
       ('rect', {'x': '12', 'y': '14', 'width': '24', 'height': '24', 'rx': '8'}, 'owl', 'sky')]
_phid = itertools.count()


def placeholder_svg(i, size=44):
    tag_, attrs, tone, bg = _PH[i]
    light, base, dark = avatars.COLORS[tone]
    ink = avatars.I
    body = avatars._body(f'ph{next(_phid)}', tag_, attrs, light, base, dark)
    face = f'<circle cx="19.5" cy="25" r="2" fill="{ink}"></circle><circle cx="28.5" cy="25" r="2" fill="{ink}"></circle>' + avatars._s('M20.5 31q3.5 2 7 0', 2.2)
    return f'<svg width="{size}" height="{size}" viewBox="0 0 48 48" aria-hidden="true" style="display: block; flex-shrink: 0;"><rect width="48" height="48" rx="{round(12 * 48 / size, 1)}" fill="var(--avatar-{bg})"></rect>{avatars._ground()}{body}{face}</svg>'


NAMES = [('scout', 'Скаут'), ('mac', 'Мак'), ('sre', 'SRE'), ('coder', 'Кодер'), ('archive', 'Архив'), ('owl', 'Сова'), ('spark', 'Искра'), ('robot', 'Робот')]


def all15(size):
    out = [(n, avatars.avatar_svg(k, size)) for k, n in NAMES]
    out += [(f'Персонаж {9 + i}', placeholder_svg(i, size)) for i in range(7)]
    return out


# ---- каркас Mac ----
def desk_side(active, scout='работает'):
    bots = ''.join(f'<a href="Thread.dc.html" style="display: flex; gap: 10px; align-items: center; min-height: 44px; padding: 0 10px; border-radius: 12px; text-decoration: none; color: var(--fg-default);">{avatar(p, l, 28)}<span style="font: 600 14px/18px {SANS};">{n}</span><span style="margin-left: auto; font: 400 12px/16px {SANS}; color: var(--fg-muted);">{s}</span></a>' for p, l, n, s in [('gemini', 'С', 'Скаут', scout), ('claude', 'М', 'Мак', 'готово'), ('claude', 'S', 'SRE', 'спит'), ('codex', 'К', 'Кодер', 'стоп')])
    nav = ''
    for i, t, k in [('clock', 'Рутины', 'routines'), ('shield', 'Решения · 2', 'approvals'), ('list', 'Активность', 'activity'), ('brain', 'Память', 'memory'), ('bars', 'Расход', 'usage'), ('plug', 'Провайдеры', 'providers'), ('sliders', 'Разрешения', 'perms'), ('user', 'Пользователи', 'users')]:
        a = k == active
        cur = ' aria-current="page"' if a else ''
        nav += f'<a href="#"{cur} style="display: flex; gap: 10px; align-items: center; min-height: 44px; padding: 0 10px; border-radius: 10px; text-decoration: none; color: var(--fg-default); background: {"var(--bg-surface)" if a else "transparent"}; border: 1px solid {"var(--border-default)" if a else "transparent"}; font: {"600" if a else "500"} 14px/18px {SANS};">{ic(i, 18)}{t}</a>'
    return f'<aside style="width: 264px; flex-shrink: 0; box-sizing: border-box; padding: 20px 14px; display: flex; flex-direction: column; gap: 2px; border-right: 1px solid var(--border-default);"><div style="{TITLE} padding: 0 10px 12px;">{BRAND}</div>{bots}<div style="height: 1px; margin: 10px; background: var(--border-default);"></div>{nav}</aside>'


def desk_head(title, sub='', right=''):
    s = f'<span style="{FN}">{sub}</span>' if sub else ''
    return f'<div style="flex-shrink: 0; padding: 18px 24px; border-bottom: 1px solid var(--border-default); display: flex; align-items: center; gap: 12px;"><div style="flex-grow: 1; min-width: 0; display: flex; flex-direction: column; gap: 2px;"><h1 style="margin: 0; {TITLE} color: var(--fg-default);">{title}</h1>{s}</div>{right}</div>'


def desk(active, title, sub, right, body, aside=None, aside_w=360, scout='работает'):
    a = f'<aside style="width: {aside_w}px; flex-shrink: 0; box-sizing: border-box; padding: 20px 16px; border-left: 1px solid var(--border-default); display: flex; flex-direction: column; gap: 12px; overflow: hidden;">{aside}</aside>' if aside else ''
    return desk_side(active, scout) + f'<main style="flex-grow: 1; min-width: 0; display: flex; flex-direction: column;">{desk_head(title, sub, right)}<div style="flex-grow: 1; min-height: 0; display: flex;"><div style="flex-grow: 1; min-width: 0; padding: 20px 24px; display: flex; flex-direction: column; gap: 12px; overflow: hidden;">{body}</div>{a}</div></main>'


def table(head, body_rows, cols):
    th = ''.join(f'<span role="columnheader" style="font: 600 12px/16px {SANS}; letter-spacing: 0.04em; text-transform: uppercase; color: var(--fg-muted);">{h}</span>' for h in head)
    out = f'<div role="row" style="display: grid; grid-template-columns: {cols}; gap: 12px; padding: 10px 16px; align-items: center;">{th}</div>'
    for r in body_rows:
        cells = ''.join(f'<span role="cell" style="min-width: 0; display: flex; align-items: center; gap: 8px; {CALL} color: var(--fg-default);">{c}</span>' for c in r)
        out += f'<div role="row" style="display: grid; grid-template-columns: {cols}; gap: 12px; padding: 8px 16px; min-height: 56px; box-sizing: border-box; align-items: center; border-top: 1px solid var(--border-default);">{cells}</div>'
    return f'<div role="table" style="{CARD} display: flex; flex-direction: column; flex-shrink: 0; overflow: hidden;">{out}</div>'


# ---- сборка артборда ----
BOARDS = []


def page(name, title, views, w=PW, h=PH_, label='Состояние'):
    """views: [(id, подпись в Tweaks, разметка)]. Первое состояние показывается по умолчанию."""
    body = ''
    for i, (vid, _, html) in enumerate(views):
        body += f'<sc-if value="{H("is_" + vid)}" hint-placeholder-val="{H("true" if i == 0 else "false")}">{html}</sc-if>\n'
    root = f'<div data-theme="{H("theme")}" style="position: relative; width: {w}px; height: {h}px; box-sizing: border-box; overflow: hidden; background: var(--bg-canvas); color: var(--fg-default); font-family: {SANS}; display: flex; flex-direction: {"column" if w < 600 else "row"};">\n{body}</div>'
    labels = [v[1] for v in views]
    props = json.dumps({"view": {"editor": "enum", "options": labels, "default": labels[0], "section": "Вид"},
                        "theme": {"editor": "enum", "options": ["light", "dark"], "default": "light", "section": "Вид"},
                        "$preview": {"width": w, "height": h}}, ensure_ascii=False).replace('&', '&amp;').replace("'", '&#39;')
    vmap = json.dumps({v[1]: v[0] for v in views}, ensure_ascii=False)
    keys = ', '.join(f"is_{v[0]}: v === '{v[0]}', go_{v[0]}: go('{v[0]}')" for v in views)
    html = f"""<!doctype html>
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
const map = {vmap};
const p = map[this.props.view] ?? '{views[0][0]}';
const s = this.state || {{}};
const v = (s.v && s.p === p) ? s.v : p;
const go = (x) => () => this.setState({{ v: x, p: p }});
return {{ theme: this.props.theme ?? 'light', {keys} }};
}}
}}
</script>
</body>
</html>
"""
    open(os.path.join(OUT, name), 'w', encoding='utf-8').write(html)
    BOARDS.append((name, title, w, h, [v[1] for v in views]))
    return name
