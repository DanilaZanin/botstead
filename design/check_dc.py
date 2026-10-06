#!/usr/bin/env python3
"""Проверка артбордов .dc.html в design/screens (формат: .ai/dc-format.md).

Статика (без зависимостей): баланс тегов, нет самозакрывающихся, <script src="./support.js">,
class Component extends DCLogic, атрибуты в двойных кавычках, {{дырки}} без операторов,
каждая дырка есть в renderVals(), события = целое значение {{функция}}, корень = $preview,
в Prototype нет ссылок <a>, у каждой <button> type="button", нет emoji и длинного тире.
Рантайм (если есть node): design/dc_runtime.mjs поднимает Component, раскрывает шаблон
и проверяет, что все дырки разрешаются, а onXxx указывают на функции.

Запуск: python3 design/check_dc.py [файл.dc.html ...] [--no-runtime]
Код выхода 1, если есть ошибки.
"""
import glob
import json
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCREENS = os.path.join(HERE, 'screens')
VOID = {'input', 'br', 'meta', 'link', 'img', 'hr', 'source', 'col', 'wbr', 'area', 'base', 'embed', 'track', 'param'}
TAG = re.compile(r'<!--.*?-->|<(/?)([A-Za-z][^\s>/]*)((?:\s+[^\s=>/"\']+(?:\s*=\s*(?:"[^"]*"|\'[^\']*\'))?)*)\s*(/?)>', re.S)
ATTR = re.compile(r'([^\s=>/"\']+)(?:\s*=\s*(?:"([^"]*)"|\'([^\']*)\'))?')
HOLE = re.compile(r'\{\{(.*?)\}\}', re.S)
PATH = re.compile(r'^[A-Za-z_$][\w$]*(\.[\w$]+)*$')
LITERAL = re.compile(r'^(true|false|null|-?\d+(\.\d+)?)$')
EMOJI = re.compile('[\U0001F000-\U0001FAFF☀-✒✕-➿⭐⭕️]')
EVENT = re.compile(r'^on[A-Z]\w*$')


def strip_raw(src):
    """Вырезает содержимое <script> и <style>, чтобы тегов внутри не искать. Возвращает (разметка, скрипт логики)."""
    script = ''
    m = re.search(r'<script type="text/x-dc" data-dc-script[^>]*>(.*?)</script>', src, re.S)
    if m:
        script = m.group(1)
    markup = re.sub(r'(<script\b[^>]*>).*?(</script>)', r'\1\2', src, flags=re.S)
    markup = re.sub(r'(<style\b[^>]*>).*?(</style>)', r'\1\2', markup, flags=re.S)
    return markup, script


def render_vals_keys(script):
    """Ключи верхнего уровня объекта, который возвращает renderVals()."""
    m = re.search(r'renderVals\s*\([^)]*\)\s*\{', script)
    if not m:
        return None
    # ищем «return {» после renderVals, но на верхнем уровне метода: последний return объекта метода
    i = m.end()
    depth, n = 1, len(script)
    ret = None
    while i < n and depth:
        c = script[i]
        if c in '"\'`':
            i = skip_string(script, i)
            continue
        if script.startswith('//', i):
            i = script.find('\n', i)
            if i < 0:
                break
            continue
        if script.startswith('/*', i):
            i = script.find('*/', i) + 2
            continue
        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
        elif depth == 1 and script.startswith('return', i) and re.match(r'return\s*\{', script[i:]):
            ret = i + re.match(r'return\s*', script[i:]).end()
        i += 1
    if ret is None:
        return None
    keys, i, depth = set(), ret + 1, 1
    expect_key = True
    while i < n and depth:
        c = script[i]
        if c in '"\'`':
            j = skip_string(script, i)
            if depth == 1 and expect_key:
                tail = script[j:j + 40].lstrip()
                if tail.startswith(':'):
                    keys.add(script[i + 1:j - 1])
                    expect_key = False
            i = j
            continue
        if script.startswith('//', i):
            i = script.find('\n', i)
            if i < 0:
                break
            continue
        if script.startswith('/*', i):
            i = script.find('*/', i) + 2
            continue
        if c in '{[(':
            depth += 1
        elif c in '}])':
            depth -= 1
        elif depth == 1 and c == ',':
            expect_key = True
        elif depth == 1 and expect_key:
            mm = re.match(r'(\.\.\.)?([A-Za-z_$][\w$]*)\s*(?=[:,}(]|\n)', script[i:])
            if mm and not mm.group(1):
                tail = script[i + len(mm.group(0)):].lstrip()
                keys.add(mm.group(2))
                expect_key = False
                if tail.startswith('('):
                    pass
                i += len(mm.group(0))
                continue
        i += 1
    return keys


def skip_string(s, i):
    q = s[i]
    i += 1
    while i < len(s):
        if s[i] == '\\':
            i += 2
            continue
        if q == '`' and s.startswith('${', i):
            d, i = 1, i + 2
            while i < len(s) and d:
                if s[i] in '"\'`':
                    i = skip_string(s, i)
                    continue
                d += (s[i] == '{') - (s[i] == '}')
                i += 1
            continue
        if s[i] == q:
            return i + 1
        i += 1
    return i


def check_file(path):
    name = os.path.basename(path)
    errors, warns = [], []
    src = open(path, encoding='utf-8').read()
    if not re.search(r'<script src="\./support\.js"></script>', src):
        errors.append('нет строки <script src="./support.js"></script>')
    markup, script = strip_raw(src)
    if not script:
        errors.append('нет <script type="text/x-dc" data-dc-script>')
    elif not re.search(r'class\s+Component\s+extends\s+DCLogic\b', script):
        errors.append('нет class Component extends DCLogic')
    if re.search(r'^\s*(import|export)\s', script, re.M):
        errors.append('import/export в скрипте логики')
    keys = render_vals_keys(script) if script else None
    if script and keys is None:
        errors.append('не нашёл return { ... } в renderVals()')
    keys = keys or set()
    for ch, label in ((EMOJI, 'emoji'), (re.compile('\\u2014'), 'длинное тире')):
        mm = ch.search(src)
        if mm:
            errors.append(f'{label} в файле: {mm.group(0)!r} (строка {src[:mm.start()].count(chr(10)) + 1})')
    if '<x-dc>' not in markup or '</x-dc>' not in markup:
        errors.append('нет <x-dc>…</x-dc>')

    # обход тегов
    stack = []  # (tag, scope-vars)
    scopes = [set()]
    seg_names = set(re.findall(r'[A-Za-z_$][\w$]*', script))
    holes = 0
    for m in TAG.finditer(markup):
        line = markup[:m.start()].count('\n') + 1
        if m.group(0).startswith('<!--'):
            continue
        closing, tag, attrs_s, selfclose = m.group(1), m.group(2), m.group(3), m.group(4)
        if selfclose:
            errors.append(f'строка {line}: самозакрывающийся тег <{tag} …/>')
        if closing:
            if not stack or stack[-1][0] != tag:
                errors.append(f'строка {line}: </{tag}> не совпадает с <{stack[-1][0] if stack else "ничего"}>')
                continue
            stack.pop()
            scopes.pop()
            continue
        attrs = [(a.group(1), a.group(2) if a.group(2) is not None else a.group(3), a.group(3) is not None and a.group(2) is None) for a in ATTR.finditer(attrs_s)]
        scope = set(scopes[-1])
        for an, av, single in attrs:
            if single and not (tag == 'script' and an == 'data-props'):
                errors.append(f'строка {line}: <{tag} {an}> в одинарных кавычках')
        if tag == 'sc-for':
            d = dict((a[0], a[1]) for a in attrs)
            if d.get('as'):
                scope.add(d['as'])
            scope.add('$index')
        if tag == 'sc-if' and not any(a[0] == 'value' for a in attrs):
            errors.append(f'строка {line}: <sc-if> без value')
        if tag == 'button' and not any(a[0] == 'type' and a[1] == 'button' for a in attrs):
            errors.append(f'строка {line}: <button> без type="button"')
        if tag == 'a' and name == 'Prototype.dc.html':
            errors.append(f'строка {line}: в Prototype нельзя ссылки <a>, только button + onClick')
        for an, av, _ in attrs:
            if av is None:
                continue
            for hm in HOLE.finditer(av):
                holes += 1
                check_hole(hm.group(1).strip(), scope, keys, seg_names, errors, line)
            if EVENT.match(an) and not re.fullmatch(r'\{\{[^{}]+\}\}', av.strip()):
                errors.append(f'строка {line}: событие {an}="{av}" должно быть целым значением {{{{функция}}}}')
        if tag not in VOID:
            stack.append((tag, None))
            scopes.append(scope)
        else:
            pass
        # дырки в тексте после тега считаются ниже
    if stack:
        errors.append('не закрыт тег <' + stack[-1][0] + '>')

    # дырки в тексте: нужен контекст scope, поэтому второй проход по позициям
    stack2, scopes2 = [], [set()]
    pos = 0
    for m in TAG.finditer(markup):
        text = markup[pos:m.start()]
        pos = m.end()
        line = markup[:m.start()].count('\n') + 1
        for hm in HOLE.finditer(text):
            holes += 1
            check_hole(hm.group(1).strip(), scopes2[-1], keys, seg_names, errors, line)
        if m.group(0).startswith('<!--'):
            continue
        closing, tag, attrs_s = m.group(1), m.group(2), m.group(3)
        if closing:
            if stack2:
                stack2.pop()
                scopes2.pop()
            continue
        scope = set(scopes2[-1])
        if tag == 'sc-for':
            d = {a.group(1): (a.group(2) if a.group(2) is not None else a.group(3)) for a in ATTR.finditer(attrs_s)}
            if d.get('as'):
                scope.add(d['as'])
            scope.add('$index')
        if tag not in VOID and not m.group(4):
            stack2.append(tag)
            scopes2.append(scope)

    # корень = $preview
    pm = re.search(r'data-props=\'([^\']*)\'', src)
    if pm:
        try:
            pv = json.loads(pm.group(1).replace('&amp;', '&').replace('&#39;', "'")).get('$preview')
        except ValueError as e:
            pv = None
            errors.append(f'data-props не разбирается как JSON: {e}')
        root = re.search(r'</helmet>\s*<div[^>]*style="([^"]*)"', markup, re.S) or re.search(r'<x-dc>\s*<div[^>]*style="([^"]*)"', markup, re.S)
        if pv and root:
            w = re.search(r'(?<![\w-])width:\s*(\d+)px', root.group(1))
            h = re.search(r'(?<![\w-])height:\s*(\d+)px', root.group(1))
            if not w or not h or (int(w.group(1)), int(h.group(1))) != (pv['width'], pv['height']):
                errors.append(f'размер корня не совпадает с $preview {pv}')
    return errors, warns, holes


def check_hole(expr, scope, keys, seg_names, errors, line):
    if not expr:
        errors.append(f'строка {line}: пустая дырка {{{{}}}}')
        return
    if LITERAL.match(expr):
        return
    if not PATH.match(expr):
        errors.append(f'строка {line}: дырка с оператором или вызовом: {{{{{expr}}}}}')
        return
    seg = expr.split('.')
    if seg[0] not in scope and seg[0] not in keys:
        errors.append(f'строка {line}: {{{{{expr}}}}}: «{seg[0]}» нет ни в renderVals(), ни среди переменных sc-for')
        return
    for s in seg[1:]:
        if s not in seg_names:
            errors.append(f'строка {line}: {{{{{expr}}}}}: поле «{s}» не встречается в скрипте')


def runtime_check(path):
    node = shutil.which('node')
    if not node:
        return None
    r = subprocess.run([node, os.path.join(HERE, 'dc_runtime.mjs'), path], capture_output=True, text=True, timeout=60)
    return r.returncode == 0, (r.stdout + r.stderr).strip()


def main(argv):
    runtime = '--no-runtime' not in argv
    files = [a for a in argv if not a.startswith('--')] or sorted(glob.glob(os.path.join(SCREENS, '*.dc.html')))
    bad = 0
    for f in files:
        errs, warns, holes = check_file(f)
        status = 'ok' if not errs else 'ОШИБКИ'
        extra = ''
        if runtime:
            rt = runtime_check(f)
            if rt is None:
                extra = ' · рантайм: node не найден, пропущен'
            elif rt[0]:
                extra = ' · рантайм: ' + rt[1]
            else:
                errs.append('рантайм: ' + rt[1].replace('\n', '; '))
                status = 'ОШИБКИ'
        print(f'{os.path.basename(f)}: {status} · дырок {holes}{extra}')
        for e in errs[:30]:
            print('   ' + e)
        if len(errs) > 30:
            print(f'   … ещё {len(errs) - 30}')
        bad += bool(errs)
    print(f'файлов: {len(files)}, с ошибками: {bad}')
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
