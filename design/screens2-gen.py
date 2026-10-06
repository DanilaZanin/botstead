#!/usr/bin/env python3
"""Генератор экранов этапа 2 (вход, провайдеры, терминал входа, браузер бота, запись и процедуры,
аватар, память, активность, разрешения): design/screens/*.dc.html плюс строки в canvas.json.

Кирпичи: screens2_lib.py. Экраны: screens2_a.py, screens2_b.py, screens2_c.py (артборды пишутся при импорте).
У каждого артборда в Tweaks переключатель «view» (состояния экрана) и «theme».
Запуск: python3 design/screens2-gen.py && python3 design/check_dc.py
"""
import json
import os

import screens2_lib as L
import screens2_a  # noqa: F401
import screens2_b  # noqa: F401
import screens2_c  # noqa: F401

ROWS = [('Вход, инвайты, первый вход, настройки, пользователи', ['Login', 'Invite', 'Welcome', 'Settings', 'AdminUsers', 'AdminUsersDesktop']),
        ('Провайдеры, модели, вход по подписке', ['Providers', 'ProviderAdd', 'ProviderModels', 'BotModel', 'CliLogin', 'ProvidersDesktop', 'CliLoginDesktop']),
        ('Браузер бота, запись, процедуры', ['Browser', 'Recording', 'RecordingReview', 'ProcedureEdit', 'Procedures', 'ProcedureRun', 'Replay', 'BrowserDesktop', 'ProceduresDesktop']),
        ('Новый бот (3 шага), память, активность, разрешения', ['NewBotDescribe', 'AvatarPick', 'NewBotConfirm', 'MemoryEntries', 'Activity', 'Schedules', 'Permissions', 'MemoryDesktop', 'ActivityDesktop', 'PermissionsDesktop'])]
Y0, PITCH, GAP = 4872, 1264, 80

path = os.path.join(L.OUT, 'canvas.json')
idx = json.load(open(path, encoding='utf-8'))
meta = {b[0]: b for b in L.BOARDS}
assert sorted(meta) == sorted(n + '.dc.html' for _, r in ROWS for n in r), 'ROWS и BOARDS расходятся'
for k in [k for k in idx['notes'] if k.startswith('s2_')]:
    del idx['notes'][k]
for ri, (title, names) in enumerate(ROWS):
    x, y = 0, Y0 + ri * PITCH
    for n in names:
        f = n + '.dc.html'
        _, t, w, h, views = meta[f]
        idx['boards'][f] = {'x': x, 'y': y, 'w': w, 'h': h, 'title': t, 'is_interactive': True}
        if f not in idx['order']:
            idx['order'].append(f)
        x += w + GAP
    idx['notes'][f's2_t{ri}'] = {'x': 0, 'y': y - 300, 'text': title, 'kind': 'title1', 'maxW': x}
idx['notes']['s2_n0'] = {'x': -420, 'y': Y0, 'w': 340, 'fill': 'blue', 'text': 'Экраны этапа 2. У каждого артборда в Tweaks параметр view: данные, пусто, загрузка, ошибка и свои состояния. '
                         'В Play кнопки переключают состояния (Перехватить, Вернуть боту, Пауза, выбор режима). Телефон 393x852 с отступом под Dynamic Island. Персонажи 9-15 в выборе аватара: плейсхолдеры.'}
if 'Desktop.dc.html' in idx['boards']:
    idx['boards']['Desktop.dc.html']['title'] = L.BRAND + ' на Mac'
json.dump(idx, open(path, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
print(len(L.BOARDS), 'артбордов,', sum(len(b[4]) for b in L.BOARDS), 'состояний')
