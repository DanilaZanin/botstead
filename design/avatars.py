# ВНИМАНИЕ: с 2026-10-05 скрипт больше не источник pwa/avatars.js. В PWA теперь растровые картинки
# pwa/avatars/<вид>.webp, а таблицу подписей в pwa/avatars.js правят вручную. Файл оставлен как справка
# по прежним векторным персонажам; токены --avatar-* из pwa/styles.css удалены.
# Персонажи ботов Bot Hub: viewBox 48x48, фон var(--avatar-<color>), объёмный "clay" стиль
# Фигура строится из radialGradient (тело) + прозрачных эллипсов (блик/тень), обрезанных
# clipPath по силуэту — без фильтров, безопасно для Safari.
import itertools

I = 'var(--avatar-ink)'


def _s(d, w=2.4):
    return f'<path d="{d}" fill="none" stroke="{I}" stroke-width="{w}" stroke-linecap="round" stroke-linejoin="round"></path>'


_uid = itertools.count()

# (светлый, основной, тёмный) тон тела — свой на каждого персонажа, в паре с фоном плитки
COLORS = {
    'scout': ('#ffd7a8', '#ffb066', '#d97f2e'),
    'mac': ('#a9eef0', '#5ec8d8', '#2f8fa0'),
    'sre': ('#ffab7a', '#e8763c', '#b8501f'),
    'coder': ('#b9a4ff', '#8a63d2', '#5c3fa0'),
    'archive': ('#e3b988', '#c48a52', '#96612f'),
    'owl': ('#dcb583', '#b98a54', '#8a5f34'),
    'spark': ('#ffe28a', '#ffcb3f', '#d9a015'),
    'robot': ('#b7c9e6', '#6f8fb8', '#47638c'),
}


def _body(u, tag, attrs, light, base, dark, cx='35%', cy='25%', stroke_w=2.4, shine=True):
    gid, cid = f'bg{u}', f'bc{u}'
    attr_str = ' '.join(f'{k}="{v}"' for k, v in attrs.items())
    grad = (f'<radialGradient id="{gid}" cx="{cx}" cy="{cy}" r="78%">'
            f'<stop offset="0%" stop-color="{light}"></stop>'
            f'<stop offset="55%" stop-color="{base}"></stop>'
            f'<stop offset="100%" stop-color="{dark}"></stop>'
            f'</radialGradient>')
    clip = f'<clipPath id="{cid}"><{tag} {attr_str}></{tag}></clipPath>'
    shape = f'<{tag} {attr_str} fill="url(#{gid})" stroke="{I}" stroke-width="{stroke_w}"></{tag}>'
    if not shine:
        return f'<defs>{grad}{clip}</defs>{shape}'
    gloss = (f'<g clip-path="url(#{cid})">'
             f'<ellipse cx="17" cy="15" rx="9" ry="6" fill="#ffffff" opacity="0.30" transform="rotate(-20 17 15)"></ellipse>'
             f'<ellipse cx="30" cy="40" rx="17" ry="8" fill="{dark}" opacity="0.28"></ellipse>'
             f'</g>')
    return f'<defs>{grad}{clip}</defs>{shape}{gloss}'


def _ground(cx=24, cy=41.5, rx=14, ry=3):
    return f'<ellipse cx="{cx}" cy="{cy}" rx="{rx}" ry="{ry}" fill="#0d1013" opacity="0.16"></ellipse>'


def _g_scout(u):
    light, base, dark = COLORS['scout']
    body = _body(u, 'circle', {'cx': '24', 'cy': '28', 'r': '13'}, light, base, dark, cx='38%', cy='24%')
    flag = (f'<path d="M24 7 L31 9.5 L24 12 Z" fill="#e35b4a"></path>'
            f'<path d="M24 15V7l7 2.5L24 12" fill="none" stroke="{I}" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"></path>')
    eye = (f'<circle cx="19" cy="27" r="5" fill="#eaf4ff" stroke="{I}" stroke-width="2.4"></circle>'
           f'<circle cx="19" cy="27" r="1.8" fill="{I}"></circle>'
           f'<circle cx="30" cy="27" r="2" fill="{I}"></circle>')
    smile = _s('M20 34q4 2.5 8 0')
    return _ground() + body + flag + eye + smile


def _g_mac(u):
    light, base, dark = COLORS['mac']
    body = _body(u, 'rect', {'x': '9', 'y': '10', 'width': '30', 'height': '21', 'rx': '5'}, light, base, dark, cx='34%', cy='20%')
    eyes = f'<rect x="16" y="16" width="3.5" height="5" rx="1" fill="{I}"></rect><rect x="28.5" y="16" width="3.5" height="5" rx="1" fill="{I}"></rect>'
    mouth = _s('M19 24.5q5 3.5 10 0')
    stand = _s('M20 31v5M28 31v5M15 37h18')
    return _ground(cy=40) + body + eyes + mouth + stand


def _g_sre(u):
    light, base, dark = COLORS['sre']
    lamp = f'<rect x="20.5" y="6" width="7" height="6" rx="2" fill="#f59e0b" stroke="{I}" stroke-width="2.2"></rect>'
    body = _body(u, 'rect', {'x': '12', 'y': '16', 'width': '24', 'height': '23', 'rx': '10'}, light, base, dark, cx='36%', cy='20%')
    brim = _s('M10 23q14-14 28 0')
    face = _s('M17.5 28h4.5M26 28h4.5') + _s('M21 34h6')
    return _ground() + lamp + body + brim + face


def _g_coder(u):
    light, base, dark = COLORS['coder']
    body = _body(u, 'rect', {'x': '10', 'y': '11', 'width': '28', 'height': '27', 'rx': '9'}, light, base, dark, cx='34%', cy='20%')
    brackets = _s('M19 20l-4 4 4 4') + _s('M29 20l4 4-4 4')
    cursor = _s('M20 32.5h5') + f'<rect x="27" y="29.5" width="2.6" height="5" rx="0.8" fill="{I}"></rect>'
    return _ground() + body + brackets + cursor


def _g_archive(u):
    light, base, dark = COLORS['archive']
    box = _body(f'{u}b', 'rect', {'x': '11', 'y': '18', 'width': '26', 'height': '20', 'rx': '3'}, light, base, dark, cx='36%', cy='18%')
    lid = _body(f'{u}a', 'rect', {'x': '8.5', 'y': '12', 'width': '31', 'height': '6.5', 'rx': '2'}, light, base, dark, cx='32%', cy='25%', shine=False)
    glasses = (f'<circle cx="19" cy="26.5" r="4" fill="none" stroke="{I}" stroke-width="2.2"></circle>'
               f'<circle cx="29" cy="26.5" r="4" fill="none" stroke="{I}" stroke-width="2.2"></circle>')
    bridge = _s('M23 26.5h2', 2.2)
    smile = _s('M21 33.5q3 1.8 6 0')
    return _ground() + box + lid + glasses + bridge + smile


def _g_owl(u):
    light, base, dark = COLORS['owl']
    tufts = f'<path d="M12 14l6 5M36 14l-6 5" fill="none" stroke="{I}" stroke-width="2.4" stroke-linecap="round"></path>'
    body = _body(u, 'ellipse', {'cx': '24', 'cy': '28', 'rx': '13', 'ry': '12'}, light, base, dark, cx='36%', cy='20%')
    eyewhite = '<circle cx="18.5" cy="26" r="4.6" fill="#fff8ec"></circle><circle cx="29.5" cy="26" r="4.6" fill="#fff8ec"></circle>'
    eyes = (f'<circle cx="18.5" cy="26" r="4.2" fill="none" stroke="{I}" stroke-width="2.2"></circle>'
            f'<circle cx="29.5" cy="26" r="4.2" fill="none" stroke="{I}" stroke-width="2.2"></circle>'
            f'<circle cx="18.5" cy="26" r="1.6" fill="{I}"></circle><circle cx="29.5" cy="26" r="1.6" fill="{I}"></circle>')
    beak = f'<path d="M22.5 31.5l1.5 2 1.5-2Z" fill="#e8a23b" stroke="{I}" stroke-width="1.6" stroke-linejoin="round"></path>'
    return _ground() + tufts + body + eyewhite + eyes + beak


def _g_spark(u):
    light, base, dark = COLORS['spark']
    rays = f'<path d="M24 6l2.2 5.5M24 6l-2.2 5.5M9 17l5 2.5M39 17l-5 2.5" fill="none" stroke="{dark}" stroke-width="2.4" stroke-linecap="round"></path>'
    body = _body(u, 'circle', {'cx': '24', 'cy': '28', 'r': '12.5'}, light, base, dark, cx='38%', cy='24%')
    face = _s('M18 26q1.5-2 3 0M27 26q1.5-2 3 0') + f'<ellipse cx="24" cy="32.5" rx="3" ry="2.2" fill="{I}"></ellipse>'
    return _ground() + rays + body + face


def _g_robot(u):
    light, base, dark = COLORS['robot']
    body = _body(u, 'rect', {'x': '11', 'y': '12', 'width': '26', 'height': '24', 'rx': '6'}, light, base, dark, cx='34%', cy='20%')
    ears = (f'<rect x="6.5" y="21" width="3" height="8" rx="1.5" fill="{dark}" stroke="{I}" stroke-width="1.6"></rect>'
            f'<rect x="38.5" y="21" width="3" height="8" rx="1.5" fill="{dark}" stroke="{I}" stroke-width="1.6"></rect>')
    antenna = _s('M24 12V7') + f'<circle cx="24" cy="6" r="2" fill="{dark}" stroke="{I}" stroke-width="1.4"></circle>'
    face = (f'<circle cx="19" cy="23" r="2.6" fill="{I}"></circle><circle cx="29" cy="23" r="2.6" fill="{I}"></circle>'
            f'<rect x="18" y="29" width="12" height="3.5" rx="1.7" fill="none" stroke="{I}" stroke-width="2.2"></rect>')
    return _ground() + body + ears + antenna + face


CHAR = {
    'scout': ('sky', 'Скаут: подзорная труба и флажок', _g_scout),
    'mac': ('peach', 'Мак: экран с пиксельными глазами', _g_mac),
    'sre': ('mint', 'SRE: каска с мигалкой', _g_sre),
    'coder': ('lilac', 'Кодер: глаза < > и курсор', _g_coder),
    'archive': ('sand', 'Архив: коробка в очках', _g_archive),
    'owl': ('lime', 'Сова: исследователь', _g_owl),
    'spark': ('peach', 'Искра: генератор идей', _g_spark),
    'robot': ('sky', 'Робот: универсальный', _g_robot),
}


def avatar_svg(kind, size=44, radius=None):
    color, label, g = CHAR[kind]
    u = next(_uid)
    r = radius if radius is not None else 12 * 48 / size if size else 12
    return f'<svg width="{size}" height="{size}" viewBox="0 0 48 48" aria-hidden="true" style="display: block; flex-shrink: 0;"><rect width="48" height="48" rx="{round(12*48/size,1)}" fill="var(--avatar-{color})"></rect>{g(u)}</svg>'


def avatar_html(kind, provider=None, size=44):
    dot = ''
    if provider:
        d = max(10, round(size * 0.3))
        dot = f'<span title="{provider}" style="position: absolute; right: -2px; bottom: -2px; width: {d}px; height: {d}px; border-radius: {d}px; background: var(--{provider}-fg); border: 2px solid var(--bg-surface); box-sizing: border-box;"></span>'
    return f'<span aria-hidden="true" style="position: relative; display: inline-block; width: {size}px; height: {size}px; flex-shrink: 0;">{avatar_svg(kind, size)}{dot}</span>'
