// Мини-рантайм .dc.html для проверок без браузера: поднимает class Component,
// вызывает renderVals() и раскрывает шаблон (sc-if, sc-for, {{дырки}}).
// Реального support.js в репозитории нет, поэтому это приближение к его семантике.
//   node design/dc_runtime.mjs FILE.dc.html   -> проверка: все {{...}} разрешаются, onXxx = функции
// Как модуль: loadComponent(file), renderHtml(file, component), parseTemplate(src).
import fs from 'node:fs';

const VOID = new Set(['input', 'br', 'meta', 'link', 'img', 'hr', 'source', 'col', 'wbr', 'area', 'base', 'embed', 'track', 'param']);
const TOKEN = /<!--[\s\S]*?-->|<\/?[A-Za-z][^\s>\/]*(?:\s+[^\s=>\/"']+(?:\s*=\s*(?:"[^"]*"|'[^']*'))?)*\s*\/?>|[^<]+|</g;
const ATTR = /([^\s=>\/"']+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'))?/g;

export function parseTemplate(src) {
  const root = { tag: '#root', attrs: [], children: [] };
  const stack = [root];
  for (const m of src.matchAll(TOKEN)) {
    const t = m[0];
    const top = stack[stack.length - 1];
    if (t.startsWith('<!--')) continue;
    if (t.startsWith('</')) {
      const name = t.slice(2, -1).trim();
      if (stack.length < 2 || stack[stack.length - 1].tag !== name) throw new Error('несовпадение закрывающего тега </' + name + '> внутри <' + stack[stack.length - 1].tag + '>');
      stack.pop();
      continue;
    }
    if (t.startsWith('<') && t.length > 1) {
      const nm = /^<([^\s>\/]+)/.exec(t)[1];
      const attrs = [];
      for (const a of t.slice(nm.length + 1, t.endsWith('/>') ? -2 : -1).matchAll(ATTR)) attrs.push([a[1], a[2] ?? a[3] ?? null]);
      const node = { tag: nm, attrs, children: [] };
      top.children.push(node);
      if (!VOID.has(nm) && !t.endsWith('/>')) stack.push(node);
      continue;
    }
    top.children.push({ text: t });
  }
  if (stack.length !== 1) throw new Error('не закрыт тег <' + stack[stack.length - 1].tag + '>');
  return root;
}

export function splitFile(file) {
  const src = fs.readFileSync(file, 'utf8');
  const tpl = /<x-dc>([\s\S]*?)<\/x-dc>/.exec(src);
  const sc = /<script type="text\/x-dc" data-dc-script[^>]*>([\s\S]*?)<\/script>/.exec(src);
  if (!tpl || !sc) throw new Error(file + ': нет <x-dc> или скрипта логики');
  return { template: tpl[1], script: sc[1] };
}

export function loadComponent(file, props = {}) {
  const { script } = splitFile(file);
  class DCLogic {
    constructor(p) { this.props = p || {}; this.state = {}; this.renders = 0; }
    setState(p) { this.state = Object.assign({}, this.state, typeof p === 'function' ? p(this.state) : p); this.renders += 1; }
    forceUpdate() { this.renders += 1; }
  }
  const Component = new Function('DCLogic', script + '\nreturn Component;')(DCLogic);
  return new Component(props);
}

const esc = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
const escAttr = (s) => esc(s).replace(/"/g, '&quot;');
const HOLE = /\{\{\s*([^{}]*?)\s*\}\}/g;

export function expand(tree, vals, forceAll = false) {
  const problems = [];
  const lookup = (path, scope) => {
    if (path === 'true') return true;
    if (path === 'false') return false;
    if (path === 'null') return null;
    if (/^-?\d+(\.\d+)?$/.test(path)) return Number(path);
    const seg = path.split('.');
    let cur = Object.prototype.hasOwnProperty.call(scope, seg[0]) ? scope[seg[0]] : vals[seg[0]];
    for (let i = 1; i < seg.length; i++) {
      if (cur === undefined || cur === null) break;
      cur = cur[seg[i]];
    }
    if (cur === undefined) problems.push('не разрешается: {{' + path + '}}');
    return cur;
  };
  const whole = (v) => { const m = /^\{\{\s*([^{}]*?)\s*\}\}$/.exec(v || ''); return m ? m[1] : null; };
  const interp = (v, scope) => v.replace(HOLE, (_, p) => { const r = lookup(p, scope); return r === undefined || r === null ? '' : String(r); });
  const events = [];
  const out = [];
  const walk = (node, scope) => {
    if (node.text !== undefined) { out.push(node.text.replace(HOLE, (_, p) => { const r = lookup(p, scope); return r === undefined || r === null ? '' : esc(r); })); return; }
    const attr = (n) => (node.attrs.find((a) => a[0] === n) || [])[1];
    if (node.tag === 'sc-if') { const v = lookup(whole(attr('value')) ?? 'false', scope);
      if (forceAll || v) node.children.forEach((c) => walk(c, scope)); return; }
    if (node.tag === 'sc-for') {
      const list = lookup(whole(attr('list')), scope);
      if (!Array.isArray(list)) { problems.push('sc-for: не массив: ' + attr('list')); return; }
      list.forEach((item, i) => node.children.forEach((c) => walk(c, Object.assign({}, scope, { [attr('as')]: item, $index: i }))));
      return;
    }
    if (node.tag === 'helmet') return;
    const parts = [];
    for (const [name, raw] of node.attrs) {
      if (raw === null) { parts.push(name); continue; }
      const w = whole(raw);
      if (w !== null) {
        const v = lookup(w, scope);
        if (/^on[A-Z]/.test(name)) { if (typeof v !== 'function') problems.push('обработчик не функция: ' + name + '=' + raw); else events.push({ name, path: w }); parts.push('data-ev-' + name.toLowerCase() + '="1"'); continue; }
        if (typeof v === 'function') { problems.push('функция в атрибуте: ' + name + '=' + raw); continue; }
        if (v === false || v === undefined || v === null) continue;
        if (v === true) { parts.push(name); continue; }
        parts.push(name + '="' + escAttr(v) + '"');
      } else parts.push(name + '="' + escAttr(interp(raw, scope)) + '"');
    }
    out.push('<' + node.tag + (parts.length ? ' ' + parts.join(' ') : '') + '>');
    if (!VOID.has(node.tag)) { node.children.forEach((c) => walk(c, scope)); out.push('</' + node.tag + '>'); }
  };
  tree.children.forEach((c) => walk(c, {}));
  return { html: out.join(''), problems, events };
}

// Страница для скриншота: helmet-стили + раскрытый шаблон.
export function renderHtml(file, comp, forceAll = false) {
  const { template } = splitFile(file);
  const tree = parseTemplate(template);
  const r = expand(tree, comp.renderVals(), forceAll);
  const head = /<helmet>([\s\S]*?)<\/helmet>/.exec(template);
  return { page: '<!doctype html><html lang="ru"><head><meta charset="utf-8">' + (head ? head[1] : '') + '</head><body>' + r.html + '</body></html>', problems: r.problems, events: r.events };
}

if (process.argv[1] && process.argv[1].endsWith('dc_runtime.mjs') && process.argv[2]) {
  const file = process.argv[2];
  try {
    const comp = loadComponent(file);
    const r = renderHtml(file, comp, true);
    const uniq = [...new Set(r.problems)];
    if (uniq.length) { console.log(uniq.slice(0, 20).join('\n')); process.exit(1); }
    console.log('ok: ' + r.events.length + ' обработчиков, ' + r.page.length + ' байт HTML');
  } catch (e) { console.log('ошибка рантайма: ' + e.message); process.exit(1); }
}
