// terminal.js: терминал входа по подписке. xterm.js (MIT) лежит в vendor/xterm/ и подключается лениво, только на экране входа.
// Если файлов нет или они не загрузились, работает простой встроенный вывод: текст без цвета, ввод с клавиатуры, вставка.
// Клавиатура: Tab не заходит в терминал (вход по клику), Esc уходит в команду, Shift+Esc выводит из терминала.
// Вывод терминала нигде не сохраняется: ни в localStorage, ни в консоль. Для поиска ссылки входа держится короткий хвост
// в памяти страницы, он стирается при закрытии терминала.

const VENDOR = 'vendor/xterm/';
export const ESCAPE_HATCH = 'Shift+Esc';

// ---------------------------------------------------------------------------
// Ленивая загрузка xterm.js
// ---------------------------------------------------------------------------
let xtermLoad = null;

function loadScript(src) {
  return new Promise((resolve, reject) => {
    const el = document.createElement('script');
    el.src = src;
    el.async = true;
    el.onload = () => resolve();
    el.onerror = () => { el.remove(); reject(new Error('script')); };
    document.head.appendChild(el);
  });
}
function loadStyle(href) {
  return new Promise((resolve, reject) => {
    const el = document.createElement('link');
    el.rel = 'stylesheet';
    el.href = href;
    el.onload = () => resolve();
    el.onerror = () => { el.remove(); reject(new Error('style')); };
    document.head.appendChild(el);
  });
}

function loadXterm() {
  if (!xtermLoad) {
    xtermLoad = (async () => {
      await Promise.all([loadStyle(`${VENDOR}xterm.css`), loadScript(`${VENDOR}xterm.js`)]);
      if (typeof window.Terminal !== 'function') throw new Error('xterm');
      try { await loadScript(`${VENDOR}addon-fit.js`); } catch { /* размер посчитаем сами */ }
      return { Terminal: window.Terminal, Fit: window.FitAddon && window.FitAddon.FitAddon };
    })().catch((err) => { xtermLoad = null; throw err; });
  }
  return xtermLoad;
}

// ---------------------------------------------------------------------------
// Разбор вывода: ссылка входа
// ---------------------------------------------------------------------------
// eslint-disable-next-line no-control-regex
const ANSI = /\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]|[\x00-\x08\x0b\x0c\x0e-\x1a\x1c-\x1f\x7f]/g;
export const stripAnsi = (text) => text.replace(ANSI, '');

// Гиперссылка OSC 8: ESC ] 8 ; параметры ; URI (BEL | ESC \). Адрес кончается перед ESC или BEL, поэтому недописанная
// ссылка на границе фрагмента (терминатора ещё нет) не совпадает. Закрывающая последовательность `ESC ] 8 ; ;` без адреса
// и ссылки не на https тоже не подходят. Claude Code печатает так длинный адрес, который в тексте переносится по строкам.
// eslint-disable-next-line no-control-regex
const OSC8_LINK = /\x1b\]8;[^;\x07\x1b]*;(https:\/\/[^\x00-\x20\x7f"'<>\x1b]+)(?=\x07|\x1b)/g;

function cleanUrl(raw) {
  const url = raw.replace(/[.,;:)\]}>]+$/, '');
  try { return new URL(url).protocol === 'https:' ? url : ''; } catch { return ''; }
}

// Ссылка входа в выводе терминала. Ищет в сыром тексте (с управляющими последовательностями) и берёт:
// 1) последнюю гиперссылку OSC 8: её адрес целый, даже если видимый текст перенесён по строкам;
// 2) иначе последнюю законченную ссылку https в тексте без управляющих последовательностей (за ней пробел или перевод
//    строки), без хвостовой пунктуации. Недописанную ссылку на границе фрагмента не берём.
// Другие схемы (javascript:, http:) не принимаем. accept(url) необязательный фильтр (например, список хостов):
// ссылка, которую он отклонил, пропускается, берётся предыдущая подходящая.
export function findLoginUrl(text, accept = () => true) {
  const pick = (candidates) => {
    for (let i = candidates.length - 1; i >= 0; i--) {
      const url = cleanUrl(candidates[i]);
      if (url && accept(url)) return url;
    }
    return '';
  };
  const osc = pick(Array.from(text.matchAll(OSC8_LINK), (m) => m[1]));
  if (osc) return osc;
  return pick(stripAnsi(text).match(/https:\/\/[^\s"'<>\x1b\x00-\x1f]+(?=\s)/g) || []);
}

// Код устройства `codex login --device-auth`: после строки «Enter this one-time code (expires in 15 minutes)» идёт код
// вида ABCD-12345 (4 и 5 символов A-Z0-9). Последний найденный; неполный код на границе фрагмента не берём (нужны ровно
// 5 символов после дефиса и граница слова). Возвращает '' если кода нет.
export function findDeviceCode(text) {
  const found = Array.from(stripAnsi(text).matchAll(/[Oo]ne-time code[\s\S]{0,120}?\b([A-Z0-9]{4}-[A-Z0-9]{5})\b/g), (m) => m[1]);
  return found.length ? found[found.length - 1] : '';
}

// ---------------------------------------------------------------------------
// Вход по подписке: сценарий и допустимые ссылки каждого CLI (docs/contracts.md §12)
// ---------------------------------------------------------------------------
// site-code: сайт показывает код, его вставляют в терминал (claude auth login, agy).
// screen-code: терминал показывает код устройства, его вводят на сайте, CLI завершается сам (codex login --device-auth).
export const LOGIN_FLOW = { claude: 'site-code', agy: 'site-code', codex: 'screen-code' };
export const loginFlow = (cli) => LOGIN_FLOW[cli] || 'site-code';

// Ссылку из вывода терминала открывает кнопка на странице, поэтому берутся только адреса входа самого провайдера:
// строка, которую напечатала страница или файл в терминале входа, не должна стать кнопкой на чужой сайт.
export const LOGIN_HOSTS = {
  claude: ['claude.ai', 'claude.com', 'platform.claude.com', 'console.anthropic.com'],
  codex: ['auth.openai.com', 'chatgpt.com'],
  agy: ['accounts.google.com'],
};

// Точное совпадение хоста (поддомены не считаются), https, без логина, пароля и нестандартного порта.
export function isLoginUrlAllowed(cli, url) {
  const hosts = LOGIN_HOSTS[cli];
  if (!hosts) return false;
  let u;
  try { u = new URL(url); } catch { return false; }
  return u.protocol === 'https:' && !u.username && !u.password && u.port === '' && hosts.includes(u.hostname);
}

// Сколько ждать ссылку (и код устройства у codex) после открытия соединения, прежде чем признать вход зависшим.
export const LINK_TIMEOUT_MS = 60000;

// ---------------------------------------------------------------------------
// Размер: колонки и строки под размер блока (ядро принимает cols 20–300, rows 5–100)
// ---------------------------------------------------------------------------
export const clampSize = (cols, rows) => ({ cols: Math.max(20, Math.min(300, cols | 0)), rows: Math.max(5, Math.min(100, rows | 0)) });

let measureCtx = null;
function cellSize(fontFamily, fontSize, lineHeight) {
  measureCtx = measureCtx || document.createElement('canvas').getContext('2d');
  measureCtx.font = `${fontSize}px ${fontFamily}`;
  const width = measureCtx.measureText('W').width || fontSize * 0.6;
  return { width, height: fontSize * lineHeight };
}

function monoFamily() {
  return getComputedStyle(document.documentElement).getPropertyValue('--font-mono').trim() || 'ui-monospace, Menlo, monospace';
}
function themeFromTokens() {
  const css = getComputedStyle(document.documentElement);
  const token = (name) => css.getPropertyValue(name).trim();
  return {
    background: token('--bg-surface'), foreground: token('--fg-default'), cursor: token('--fg-default'), cursorAccent: token('--bg-surface'),
    selectionBackground: token('--focus'), black: token('--fg-default'), red: token('--danger-fg'), green: token('--success-fg'),
    yellow: token('--attention-fg'), blue: token('--focus'), magenta: token('--gemini-fg'), cyan: token('--codex-fg'), white: token('--fg-muted'),
  };
}

// ---------------------------------------------------------------------------
// Клавиши → байты (для встроенного вывода и для кнопок ряда)
// ---------------------------------------------------------------------------
export const KEY_BYTES = { Escape: '\x1b', Tab: '\t', ArrowUp: '\x1b[A', ArrowDown: '\x1b[B', ArrowRight: '\x1b[C', ArrowLeft: '\x1b[D', Enter: '\r', Backspace: '\x7f' };

// Ctrl + буква: управляющий код. Остальное остаётся как есть.
export function applyCtrl(data) {
  if (data.length === 1) {
    const code = data.toUpperCase().charCodeAt(0);
    if (code >= 64 && code <= 95) return String.fromCharCode(code - 64);
  }
  const arrow = { '\x1b[A': '\x1b[1;5A', '\x1b[B': '\x1b[1;5B', '\x1b[C': '\x1b[1;5C', '\x1b[D': '\x1b[1;5D' }[data];
  return arrow || data;
}

// ---------------------------------------------------------------------------
// Терминал на xterm.js
// ---------------------------------------------------------------------------
function createXterm(host, lib, opts) {
  const fontFamily = monoFamily();
  const fontSize = opts.mobile ? 12 : 13;
  const lineHeight = opts.mobile ? 1.5 : 1.35;
  const term = new lib.Terminal({
    fontFamily, fontSize, lineHeight, cols: 80, rows: 24, scrollback: 500, cursorBlink: false, convertEol: false,
    theme: themeFromTokens(), allowProposedApi: false,
  });
  const fit = lib.Fit ? new lib.Fit() : null;
  if (fit) term.loadAddon(fit);
  term.open(host);
  if (term.textarea) {
    term.textarea.setAttribute('aria-label', 'Ввод в терминал входа');
    term.textarea.tabIndex = -1; // в терминал входят кликом: Tab его не захватывает
  }
  term.attachCustomKeyEventHandler((event) => {
    if (event.type === 'keydown' && event.key === 'Escape' && event.shiftKey) { opts.onEscape(); return false; }
    return true;
  });
  const dataSub = term.onData((data) => opts.onData(data));
  const scheme = window.matchMedia('(prefers-color-scheme: dark)');
  const onScheme = () => { term.options.theme = themeFromTokens(); };
  scheme.addEventListener('change', onScheme);
  let last = { cols: 0, rows: 0 };
  function fitNow() {
    let size = null;
    const proposed = fit && fit.proposeDimensions();
    if (proposed && proposed.cols && proposed.rows) size = proposed;
    else {
      const cell = cellSize(fontFamily, fontSize, lineHeight);
      const box = host.getBoundingClientRect();
      size = { cols: Math.floor((box.width - 16) / cell.width), rows: Math.floor((box.height - 16) / cell.height) };
    }
    const { cols, rows } = clampSize(size.cols, size.rows);
    if (cols !== term.cols || rows !== term.rows) term.resize(cols, rows);
    if (cols !== last.cols || rows !== last.rows) { last = { cols, rows }; opts.onResize(cols, rows); }
  }
  return {
    kind: 'xterm',
    write: (bytes) => term.write(bytes),
    focus: () => term.focus(),
    blur: () => term.blur(),
    fit: fitNow,
    dispose() {
      scheme.removeEventListener('change', onScheme);
      dataSub.dispose();
      term.dispose();
    },
  };
}

// ---------------------------------------------------------------------------
// Встроенный вывод без xterm.js: \r, \n, \b, табуляция; управляющие последовательности отбрасываются
// ---------------------------------------------------------------------------
function createPlain(host, opts) {
  const fontSize = opts.mobile ? 12 : 13;
  const lineHeight = opts.mobile ? 1.5 : 1.35;
  const view = document.createElement('pre');
  view.className = 'term-plain';
  view.setAttribute('data-terminal-plain', '');
  view.style.fontSize = `${fontSize}px`;
  view.style.lineHeight = String(lineHeight);
  const input = document.createElement('textarea');
  input.className = 'term-plain-input';
  input.setAttribute('aria-label', 'Ввод в терминал входа');
  input.tabIndex = -1; // в терминал входят кликом: Tab его не захватывает
  input.setAttribute('autocomplete', 'off');
  input.setAttribute('autocapitalize', 'none');
  input.setAttribute('autocorrect', 'off');
  input.setAttribute('spellcheck', 'false');
  host.append(view, input);

  const decoder = new TextDecoder();
  const MAX_LINES = 500;
  let lines = [[]];
  let col = 0;
  let held = '';
  const cursor = document.createElement('span');
  cursor.className = 'term-plain-cursor';
  cursor.setAttribute('aria-hidden', 'true');

  function put(ch) {
    const line = lines[lines.length - 1];
    while (line.length < col) line.push(' ');
    line[col] = ch;
    col += 1;
  }
  function render() {
    view.textContent = lines.map((l) => l.join('')).join('\n');
    view.appendChild(cursor);
    host.scrollTop = host.scrollHeight;
  }
  function write(bytes) {
    let text = held + decoder.decode(bytes, { stream: true });
    const tail = text.match(/\x1b(?:\[[0-9;?]*[ -/]*|\][^\x07\x1b]*)?$/);
    held = tail ? tail[0] : '';
    if (tail) text = text.slice(0, -held.length);
    for (const ch of stripAnsiKeepLines(text)) {
      if (ch === '\r') col = 0;
      else if (ch === '\n') { lines.push([]); col = 0; if (lines.length > MAX_LINES) lines = lines.slice(-MAX_LINES); }
      else if (ch === '\b') col = Math.max(0, col - 1);
      else if (ch === '\t') { const next = (Math.floor(col / 8) + 1) * 8; while (col < next) put(' '); }
      else put(ch);
    }
    render();
  }
  function stripAnsiKeepLines(text) {
    return text.replace(ANSI_KEEP, '');
  }

  host.addEventListener('click', () => input.focus());
  input.addEventListener('input', () => {
    const value = input.value;
    input.value = '';
    if (value) opts.onData(value);
  });
  input.addEventListener('paste', (e) => {
    const text = e.clipboardData && e.clipboardData.getData('text');
    if (text) { e.preventDefault(); opts.onData(text); }
  });
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && e.shiftKey) { e.preventDefault(); opts.onEscape(); return; }
    let data = KEY_BYTES[e.key];
    if (!data && e.ctrlKey && !e.metaKey && !e.altKey && e.key.length === 1) data = applyCtrl(e.key);
    if (!data) return;
    e.preventDefault();
    opts.onData(data);
  });

  let last = { cols: 0, rows: 0 };
  function fitNow() {
    const cell = cellSize(monoFamily(), fontSize, lineHeight);
    const box = host.getBoundingClientRect();
    const { cols, rows } = clampSize(Math.floor((box.width - 16) / cell.width), Math.floor((box.height - 16) / cell.height));
    if (cols !== last.cols || rows !== last.rows) { last = { cols, rows }; opts.onResize(cols, rows); }
  }
  render();
  return {
    kind: 'plain',
    write,
    focus: () => input.focus(),
    blur: () => input.blur(),
    fit: fitNow,
    dispose() { lines = [[]]; held = ''; view.remove(); input.remove(); },
  };
}
// Всё, что не печатается, кроме \r \n \b \t.
// eslint-disable-next-line no-control-regex
const ANSI_KEEP = /\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]|[\x00-\x07\x0b\x0c\x0e-\x1a\x1c-\x1f\x7f]/g;

// Создаёт терминал в host. opts: mobile, onData(string), onResize(cols, rows), onEscape().
export async function createTerminal(host, opts) {
  try {
    const lib = await loadXterm();
    return createXterm(host, lib, opts);
  } catch {
    host.replaceChildren();
    return createPlain(host, opts);
  }
}
