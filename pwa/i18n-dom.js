// pwa/i18n-dom.js: DOM-переводчик интерфейса. При языке не «ru» переводит готовый DOM по каталогам en-*.js,
// поэтому экраны остаются на русских строках в исходниках. Ядро (createTranslator) не трогает DOM и гоняется в Node.
//
// Что переводится: текстовые узлы и атрибуты aria-label, title, placeholder, alt, data-tooltip, а также value у
// кнопок input (button/submit/reset). Что не переводится: содержимое script, style, code, pre, noscript, textarea,
// contenteditable и любого элемента с data-i18n-skip (свои атрибуты aria-label/title у data-i18n-skip переводятся),
// value у текстовых полей и option (это данные). Пользовательский контент (имена ботов, сообщения, память, адреса,
// терминал, браузер) помечается data-i18n-skip в разметке экранов. Своя запись узла запоминается (WeakMap), поэтому
// наблюдатель не зацикливается; кэш строк ограничен 5000.
//
// Ключи каталога с ${...} превращаются в регулярные выражения: литеральные куски экранируются, каждый ${...}
// становится группой (.*?). Английское значение собирается из захваченных кусков, см. compileTemplate.
import { getLang, getCatalog } from './i18n.js';

const CYRILLIC = /[А-Яа-яЁё]/;
const CACHE_LIMIT = 5000;
const ATTRS = ['aria-label', 'title', 'placeholder', 'alt', 'value', 'data-tooltip'];
const BUTTON_INPUTS = new Set(['button', 'submit', 'reset']);
// SKIP_SELF: у таких элементов не переводится ничего, включая свои атрибуты. data-i18n-skip закрывает только содержимое:
// собственные aria-label/title элемента остаются интерфейсным текстом (терминал входа, поле с адресом).
const SKIP_SELF = 'script,style,code,pre,noscript,[contenteditable]:not([contenteditable="false"])';
const SKIP_SELECTOR = `${SKIP_SELF},[data-i18n-skip]`;
const DEBUG_STORAGE_KEY = 'bothub.i18n.debug';

// ---------------------------------------------------------------------------
// Разбор шаблонов `... ${expr} ...`
// ---------------------------------------------------------------------------

// Индекс закрывающей кавычки строки, начавшейся в позиции i (кавычка s[i]); для ` учитывает вложенные ${...}.
function skipString(s, i) {
  const q = s[i];
  let j = i + 1;
  while (j < s.length) {
    const c = s[j];
    if (c === '\\') { j += 2; continue; }
    if (q === '`' && c === '$' && s[j + 1] === '{') { j = skipBraces(s, j + 2); continue; }
    if (c === q) return j;
    j += 1;
  }
  return s.length - 1;
}

// Индекс сразу после парной «}» (начало выражения в позиции i, сама «${» уже пропущена).
function skipBraces(s, i) {
  let depth = 1;
  let j = i;
  while (j < s.length && depth > 0) {
    const c = s[j];
    if (c === '\'' || c === '"' || c === '`') { j = skipString(s, j) + 1; continue; }
    if (c === '{') depth += 1;
    else if (c === '}') depth -= 1;
    j += 1;
  }
  return j;
}

// 'a ${x} b ${y}' → { parts: ['a ', ' b ', ''], exprs: ['x', 'y'] }.
export function splitTemplate(s) {
  const parts = [];
  const exprs = [];
  let buf = '';
  let i = 0;
  while (i < s.length) {
    if (s[i] === '$' && s[i + 1] === '{') {
      const end = skipBraces(s, i + 2);
      parts.push(buf);
      buf = '';
      exprs.push(s.slice(i + 2, end - 1));
      i = end;
    } else {
      buf += s[i];
      i += 1;
    }
  }
  parts.push(buf);
  return { parts, exprs };
}

// Содержимое строковых литералов выражения (', ", `), по порядку, без вложенных в ${} внутри `.
function scanLiterals(expr) {
  const out = [];
  let i = 0;
  while (i < expr.length) {
    const c = expr[i];
    if (c === '\'' || c === '"' || c === '`') {
      const end = skipString(expr, i);
      out.push(expr.slice(i + 1, end));
      i = end + 1;
    } else {
      i += 1;
    }
  }
  return out;
}

const escapeRe = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
const normalizeSpaces = (s) => s.replace(/[\s ]+/g, ' ').trim();
const identifiers = (expr) => new Set((expr.replace(/(['"`]).*?\1/g, ' ').match(/[A-Za-z_$][\w$]*/g)) || []);

// Выражение читается как поле данных пользователя (имя, адрес, id): захваченный кусок подставляется как есть.
const DATA_PATH = /^(?:esc\()?\s*[A-Za-z_$][\w$]*(?:\??\.[A-Za-z_$][\w$]*|\[[^\]]+\])*\s*(?:(?:\|\||\?\?)\s*(?:'[^']*'|"[^"]*"|[A-Za-z_$][\w$]*(?:\??\.[A-Za-z_$][\w$]*)*))?\s*\)?$/;
const DATA_FIELD = /(?:^|[.\s(])(?:name|display_name|email|host|url|base_url|address|note|prompt|instructions|role|summary|cron|model|id|path|origin|keyHint|tail|location)(?:\s|\)|$|\|)/;
function isDataExpr(expr) {
  const e = expr.trim();
  if (!DATA_PATH.test(e)) return false;
  return DATA_FIELD.test(e) || /^(?:esc\()?\s*(?:name|email|host|url|id)\s*\)?$/.test(e);
}

// ---------------------------------------------------------------------------
// Шаблонный перевод
// ---------------------------------------------------------------------------

// Выражение выбора из готовых литералов: plural(n, 'а', 'б', 'в') или цепочка `cond ? 'а' : cond2 ? 'б' : 'в'`.
// Для него группа регулярки сужается до перечня литералов, поэтому шаблон вида `${n} ${plural(...)}` не цепляет чужие строки.
const PLURAL_CALL = /^plural\([^,]+,\s*(?:'[^']*'|"[^"]*")(?:\s*,\s*(?:'[^']*'|"[^"]*"))*\s*\)$/;
const TERNARY_LITERALS = /^(?:[^?:'"`]+\?\s*(?:'[^']*'|"[^"]*")\s*:\s*)+(?:'[^']*'|"[^"]*")$/;
function choiceLiterals(expr) {
  const e = expr.trim();
  if (!PLURAL_CALL.test(e) && !TERNARY_LITERALS.test(e)) return null;
  return scanLiterals(e);
}

// Собирает правило из пары «русский шаблон → английский шаблон». Возвращает null, если пару не удалось связать.
// Связь выражений английского значения с ключом: (1) то же выражение; (2) наибольшее пересечение идентификаторов;
// (3) по порядку, если выражений поровну. Строковые литералы связанных выражений дают словарь ру→en
// для ternary и plural(): захваченное «бота» → 'bots'. Литералы-шаблоны (`, черновиков ${n}`) становятся вложенными правилами.
// Правило «слабое», если литералов вокруг выражений меньше трёх значащих символов: тогда любой захват с кириллицей
// должен переводиться целиком (иначе получилась бы полуанглийская строка).
export function compileTemplate(keyTpl, valTpl, depth = 0) {
  const kp = splitTemplate(keyTpl);
  const vp = splitTemplate(valTpl);
  if (!kp.exprs.length) return null;
  const literalChars = kp.parts.join('');
  const significant = literalChars.replace(/\s/g, '').length;
  const choices = kp.exprs.map(choiceLiterals);
  if (significant < 1 && !choices.some(Boolean)) return null;

  const used = new Set();
  const pairing = vp.exprs.map(() => -1);
  vp.exprs.forEach((ve, vi) => {
    const j = kp.exprs.findIndex((ke, kj) => !used.has(kj) && ke === ve);
    if (j >= 0) { pairing[vi] = j; used.add(j); }
  });
  vp.exprs.forEach((ve, vi) => {
    if (pairing[vi] >= 0) return;
    const ids = identifiers(ve);
    let best = -1;
    let bestScore = 0;
    kp.exprs.forEach((ke, kj) => {
      if (used.has(kj)) return;
      let score = 0;
      identifiers(ke).forEach((id) => { if (ids.has(id)) score += 1; });
      if (score > bestScore) { best = kj; bestScore = score; }
    });
    if (best >= 0) { pairing[vi] = best; used.add(best); }
  });
  if (kp.exprs.length === vp.exprs.length) {
    vp.exprs.forEach((ve, vi) => {
      if (pairing[vi] >= 0) return;
      const j = kp.exprs.findIndex((ke, kj) => !used.has(kj));
      if (j >= 0) { pairing[vi] = j; used.add(j); }
    });
  }
  if (pairing.some((j) => j < 0)) return null;

  const resolvers = vp.exprs.map((ve, vi) => {
    const j = pairing[vi];
    const ke = kp.exprs[j];
    const resolver = { j, raw: ke === ve && isDataExpr(ke), lits: new Map(), subs: [] };
    if (ke !== ve) {
      const kl = scanLiterals(ke);
      const vl = scanLiterals(ve);
      if (kl.length === vl.length) {
        kl.forEach((ru, li) => {
          const en = vl[li];
          if (!CYRILLIC.test(ru) || ru === en) return;
          if (ru.includes('${') && depth < 3) {
            const sub = compileTemplate(ru, en, depth + 1);
            if (sub) resolver.subs.push(sub);
          } else {
            resolver.lits.set(ru, en);
          }
        });
      }
    }
    return resolver;
  });

  const groups = choices.map((alts) => {
    if (!alts) return '(.*?)';
    const uniq = Array.from(new Set(alts)).sort((a, b) => b.length - a.length);
    return `(${uniq.map(escapeRe).join('|')})`;
  });
  let src = '^';
  // Совпадение идёт по тексту со схлопнутыми пробелами, поэтому и литералы ключа (например, двойной пробел) схлопываем.
  const keyParts = kp.parts.map((part) => part.replace(/[\s\u00a0]+/g, ' '));
  keyParts.forEach((part, i) => { src += escapeRe(part) + (i < groups.length ? groups[i] : ''); });
  const re = new RegExp(`${src}$`, 's');
  const needle = keyParts.reduce((a, b) => (b.length > a.length ? b : a), '').trim();
  const weak = significant < 3;
  return {
    re,
    needle,
    weight: significant,
    build(match, translate) {
      let out = vp.parts[0];
      for (let vi = 0; vi < resolvers.length; vi += 1) {
        const r = resolvers[vi];
        const text = match[r.j + 1];
        let value = text;
        if (!r.raw && text !== '') {
          if (r.lits.has(text)) {
            value = r.lits.get(text);
          } else {
            let hit = null;
            for (const sub of r.subs) {
              hit = sub.apply(text, translate);
              if (hit !== null) break;
            }
            if (hit === null) {
              hit = translate(text);
              if (hit === null) {
                if (weak && CYRILLIC.test(text)) return null;
                hit = text;
              }
            }
            value = hit;
          }
        }
        out += value + vp.parts[vi + 1];
      }
      return out;
    },
    apply(text, translate) {
      const m = this.re.exec(text);
      return m ? this.build(m, translate) : null;
    },
  };
}

// ---------------------------------------------------------------------------
// Ядро: строка → перевод (null, если перевода нет)
// ---------------------------------------------------------------------------

export function createTranslator(catalog) {
  const exact = new Map();
  const templates = [];
  for (const [key, value] of catalog) {
    if (typeof key !== 'string' || typeof value !== 'string') continue;
    if (key.includes('${')) {
      const rule = compileTemplate(key, value);
      if (rule) templates.push(rule);
    } else {
      exact.set(key, value);
      const norm = normalizeSpaces(key);
      if (norm !== key && !exact.has(norm)) exact.set(norm, value);
    }
  }
  templates.sort((a, b) => b.weight - a.weight);

  const cache = new Map();
  const remember = (k, v) => {
    if (cache.size >= CACHE_LIMIT) cache.delete(cache.keys().next().value);
    cache.set(k, v);
  };

  function lookup(core) {
    const direct = exact.get(core);
    if (direct !== undefined) return direct;
    const norm = normalizeSpaces(core);
    const viaNorm = exact.get(norm);
    if (viaNorm !== undefined) return viaNorm;
    for (const rule of templates) {
      if (rule.needle && !norm.includes(rule.needle)) continue;
      const m = rule.re.exec(norm);
      if (!m) continue;
      const out = rule.build(m, translateCore);
      if (out !== null && out !== norm) return out;
    }
    return null;
  }

  function translateCore(core) {
    if (!core || !CYRILLIC.test(core)) return null;
    if (cache.has(core)) return cache.get(core);
    const res = lookup(core);
    remember(core, res);
    return res;
  }

  // Переводит строку с сохранением пробелов по краям; null: перевода нет.
  function translate(text) {
    if (typeof text !== 'string' || !CYRILLIC.test(text)) return null;
    const core = text.trim();
    if (!core) return null;
    const res = translateCore(core);
    if (res === null) return null;
    const lead = text.slice(0, text.indexOf(core));
    const trail = text.slice(lead.length + core.length);
    return `${lead}${res}${trail}`;
  }

  return { translate, size: () => ({ exact: exact.size, templates: templates.length, cache: cache.size }) };
}

// ---------------------------------------------------------------------------
// DOM
// ---------------------------------------------------------------------------

let translator = null;
let observer = null;
let debugOn = false;
const missing = new Set();
// Что мы записали: узел или атрибут, чьё текущее значение равно записанному, повторно не обрабатывается (защита от цикла).
const textState = new WeakMap();
const attrState = new WeakMap();

function debugRequested() {
  try {
    if (/[?&]i18n=debug(?:&|$)/.test(location.search)) return true;
    return localStorage.getItem(DEBUG_STORAGE_KEY) === '1';
  } catch {
    return false;
  }
}

function noteMissing(text) {
  if (!debugOn) return;
  const core = normalizeSpaces(text);
  if (!core || missing.has(core)) return;
  missing.add(core);
  window.__i18nMissing.push(core);
}

function translateTextNode(node) {
  const cur = node.data;
  const st = textState.get(node);
  if (st && st.out === cur) return;
  if (!cur || !CYRILLIC.test(cur)) return;
  const parent = node.parentElement;
  if (parent && (parent.closest(SKIP_SELECTOR) || parent.closest('textarea'))) return;
  const res = translator.translate(cur);
  if (res === null) { noteMissing(cur); return; }
  if (res !== cur) {
    node.data = res;
    textState.set(node, { out: res });
  }
}

function attrAllowed(el, attr) {
  if (attr !== 'value') return true;
  return el.tagName === 'INPUT' && BUTTON_INPUTS.has((el.getAttribute('type') || '').toLowerCase());
}

function translateAttrs(el, only) {
  if (el.matches(SKIP_SELF) || (el.parentElement && el.parentElement.closest(SKIP_SELECTOR))) return;
  let states = attrState.get(el);
  for (const attr of (only ? [only] : ATTRS)) {
    if (!el.hasAttribute(attr) || !attrAllowed(el, attr)) continue;
    const cur = el.getAttribute(attr);
    if (states && states.get(attr) === cur) continue;
    if (!CYRILLIC.test(cur)) continue;
    const res = translator.translate(cur);
    if (res === null) { noteMissing(cur); continue; }
    if (res !== cur) {
      el.setAttribute(attr, res);
      if (!states) { states = new Map(); attrState.set(el, states); }
      states.set(attr, res);
    }
  }
}

function visit(node) {
  if (node.nodeType === Node.TEXT_NODE) { translateTextNode(node); return; }
  if (node.nodeType !== Node.ELEMENT_NODE) return;
  translateAttrs(node);
  if (node.matches(SKIP_SELECTOR)) return;
  for (let child = node.firstChild; child; child = child.nextSibling) visit(child);
}

// Обходит добавленное поддерево; всё внутри пропускаемых контейнеров пропускается целиком.
function walk(root) {
  if (root.nodeType === Node.ELEMENT_NODE && root.parentElement && root.parentElement.closest(SKIP_SELECTOR)) return;
  visit(root);
}

function onMutations(records) {
  for (const r of records) {
    if (r.type === 'childList') r.addedNodes.forEach(walk);
    else if (r.type === 'characterData') translateTextNode(r.target);
    else if (r.type === 'attributes') translateAttrs(r.target, r.attributeName);
  }
}

export function isTranslating() {
  return observer !== null;
}

// Переводит строку напрямую (для кода, который собирает текст вне DOM); без активного переводчика возвращает исходную.
export function translateString(text) {
  if (!translator) return text;
  const res = translator.translate(text);
  return res === null ? text : res;
}

// Запускает переводчик, если язык не «ru» и каталог загружен (после loadLang). Возвращает true, если запущен.
export function startTranslator() {
  if (observer || getLang() === 'ru') return false;
  const catalog = getCatalog();
  if (!catalog || !catalog.size) return false;
  translator = createTranslator(catalog);
  debugOn = debugRequested();
  if (debugOn && !Array.isArray(window.__i18nMissing)) window.__i18nMissing = [];
  const root = document.documentElement;
  const description = document.querySelector('meta[name="description"]');
  if (description) {
    const res = translator.translate(description.getAttribute('content') || '');
    if (res) description.setAttribute('content', res);
  }
  walk(root);
  observer = new MutationObserver(onMutations);
  observer.observe(root, {
    childList: true,
    subtree: true,
    characterData: true,
    attributes: true,
    attributeFilter: ATTRS,
  });
  return true;
}

export function stopTranslator() {
  if (observer) observer.disconnect();
  observer = null;
  translator = null;
}
