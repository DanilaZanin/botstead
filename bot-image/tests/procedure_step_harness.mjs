// Стенд для test_procedure_step.py: подменяет Playwright моделью страницы и гоняет настоящий main() из procedure-step.mjs.
// stdin: {"world": {...}, "envelope": {...} | "raw": "<текст как есть>"}; stdout: {"output": "<stdout скрипта>", "calls": [...]}.
//
// Действия идут только через ElementHandle (locator.elementHandle()): у locator нет click/fill/press/selectOption, любая попытка
// действовать через locator падает TypeError. Модель страницы умеет переходы между сверкой и действием:
//   world.url_after_handle: адрес меняется сразу после выдачи handle (handle при этом отцеплен: ownerFrame() === null);
//   world.url_after_handle_attached: то же, но handle остаётся «живым» (ловит только сверка адреса);
//   world.url_before_action: адрес меняется на первой проверке видимости (между выдачей handle и действием), handle живой;
//   world.url_during_action: адрес меняется внутри действия, главный фрейм шлёт framenavigated, действие при этом выполнено;
//   world.url_during_action_throw: то же, но действие кончается ошибкой (handle отцеплен);
//   world.url_during_keyboard: то же для page.keyboard.press;
//   world.url_on_subscribe / url_on_subscribe_silent: адрес меняется в момент подписки на framenavigated (с событием и без);
//   world.url_on_wait_for: адрес меняется, пока чтение ждёт цель (settle_ms): новая страница дозагрузилась за время ожидания;
//   element.in_iframe: ownerFrame() не главный фрейм;
//   element.enabled / element.editable = false: isEnabled() / isEditable() дают false.
// Адрес цели CDP (Target.getTargetInfo через context.newCDPSession(page)) отдельно от адреса фрейма (page.url()):
//   world.targets: адреса целей по вкладкам (параллельно world.pages); без него цель вкладки равна её page.url();
//   world.fail.cdp_session: newCDPSession падает; world.goto_lands_on_error: goto «успешен», но фрейм на chrome-error://.
import { main } from '../procedure-step.mjs';

const chunks = [];
for await (const chunk of process.stdin) chunks.push(chunk);
const { world = {}, envelope, raw } = JSON.parse(Buffer.concat(chunks).toString('utf8'));
const calls = [];
let out = '';
const exitCodes = [];
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function elementsFor(role, name) {
  return (world.elements || []).filter((el) => el.role === role && (name === undefined || el.name === name));
}

const fail = (op) => {
  if (world.fail && world.fail[op]) {
    const error = new Error(world.fail[op]);
    error.name = 'TimeoutError';
    throw error;
  }
};
const optsOf = (opts = {}) => ({ timeout: opts.timeout ?? null, noWaitAfter: opts.noWaitAfter ?? null, force: opts.force ?? null });

let acting = null;  // страница, на которой идёт вызов: для проверки, куда ушло действие
const currentUrl = () => (acting ? acting.url() : null);
const listeners = [];  // framenavigated главного фрейма основной вкладки
const navigate = (url) => {
  world.url = url;
  for (const listener of [...listeners]) listener(pages[0].mainFrame());
};

// Handle элемента: все действия идут через него. detached: после перехода handle отцеплен.
function handleFor(record, label, owner) {
  const state = { detached: false };
  const stillHere = () => { if (state.detached) throw new Error('Element is not attached to the DOM'); };
  const during = async (kind) => {
    const url = world[`url_during_${kind}`];
    if (!url) return;
    navigate(url);
    if (world[`url_during_${kind}_throw`]) { state.detached = true; throw new Error('Execution context was destroyed SECRET-PAGE-TEXT'); }
  };
  const handle = {
    state,
    async ownerFrame() { return state.detached ? null : (record.in_iframe ? { child: true } : owner.mainFrame()); },
    async evaluate() { return !state.detached; },
    async isVisible() {
      if (world.url_before_action) { const url = world.url_before_action; world.url_before_action = null; navigate(url); }
      return !state.detached && record.visible !== false;
    },
    async isEnabled() { return !state.detached && record.enabled !== false; },
    async isEditable() { return !state.detached && record.enabled !== false && record.editable !== false; },
    async click(opts) {
      stillHere();
      calls.push({ op: 'click', label, page: currentUrl(), opts: optsOf(opts) });
      if (world.hang_click) await new Promise(() => {});
      fail('click');
      await during('action');
    },
    async fill(value, opts) {
      stillHere();
      calls.push({ op: 'fill', label, value, page: currentUrl(), opts: optsOf(opts) });
      if (world.unhandled_on_fill) { Promise.reject(new Error(`unhandled ${value}`)); await sleep(30); }
      if (world.uncaught_on_fill) { setTimeout(() => { throw new Error(`boom ${value}`); }, 0); await sleep(30); }
      fail('fill');
      if (world.url_after_fill) world.url = world.url_after_fill;
      await during('action');
    },
    async press(key, opts) { stillHere(); calls.push({ op: 'press', label, key, opts: optsOf(opts) }); fail('press'); await during('action'); },
    async selectOption(value, opts) { stillHere(); calls.push({ op: 'select', label, value, opts: optsOf(opts) }); fail('select'); await during('action'); },
    async innerText() { stillHere(); return record.text || ''; },
  };
  return handle;
}

function locator(records, label, owner) {
  const one = (items) => locator(items, label, owner);
  return {
    async count() { return records.length; },
    nth(i) { return one(records.slice(i, i + 1)); },
    first() { return one(records.slice(0, 1)); },
    async isVisible() { return records.length > 0 && records[0].visible !== false; },
    async waitFor(opts) {
      calls.push({ op: 'waitFor', label, state: opts.state, timeout: opts.timeout ?? null });
      fail('waitFor');
      if (world.url_on_wait_for) { const url = world.url_on_wait_for; world.url_on_wait_for = null; navigate(url); }
    },
    async elementHandle(opts) {
      calls.push({ op: 'elementHandle', label, opts: optsOf(opts) });
      fail('elementHandle');
      if (!records[0]) throw new Error('Timeout waiting for element');
      const handle = handleFor(records[0], label, owner);
      if (world.url_after_handle) { handle.state.detached = true; navigate(world.url_after_handle); }
      if (world.url_after_handle_attached) navigate(world.url_after_handle_attached);
      return handle;
    },
    async ariaSnapshot() { if (!records[0] || !records[0].aria) throw new Error('no snapshot'); return records[0].aria; },
  };
}

const moved = [];  // вкладки, на которые ушёл goto: адрес фрейма и цели после него
const cdpCalls = [];
function makePage(baseUrl, isMain, index) {
  const getUrl = () => (moved[index] ? moved[index].frame : baseUrl());
  const frame = { url: getUrl };
  const self = {
    url: getUrl,
    async goto(url) {
      calls.push(world.targets ? { op: 'goto', url, tab: index } : { op: 'goto', url });
      world.url = url;
      if (world.fail && world.fail.goto) throw new Error(world.fail.goto);
      moved[index] = { frame: world.goto_lands_on_error ? 'chrome-error://chromewebdata/' : url, target: url };
    },
    getByRole(role, opts = {}) { acting = self; return locator(elementsFor(role, opts.name), `${role}|${opts.name ?? '*'}`, self); },
    locator(selector) {
      acting = self;
      calls.push({ op: 'locator', arg: selector });
      const bare = selector.startsWith('css=') ? selector.slice(4) : selector;
      return locator((world.selectors || {})[bare] || [], `css|${bare}`, self);
    },
    getByText(text) { return locator((world.texts || {})[text] ? [{ visible: true }] : [], `text|${text}`, self); },
    mainFrame() { return frame; },
    async waitForLoadState(state, opts = {}) { calls.push({ op: 'loadState', state, timeout: opts.timeout ?? null }); },
    on(event, listener) {
      if (event === 'framenavigated' && isMain) listeners.push(listener);
      calls.push({ op: 'on', event });
      if (world.url_on_subscribe) navigate(world.url_on_subscribe);  // переход сразу после подписки, до проверки
      if (world.url_on_subscribe_silent) world.url = world.url_on_subscribe_silent;  // адрес сменился без события
    },
    off(event, listener) {
      const at = listeners.indexOf(listener);
      if (event === 'framenavigated' && at >= 0) listeners.splice(at, 1);
      calls.push({ op: 'off', event });
    },
    keyboard: {
      async press(key, opts) {
        calls.push({ op: 'keyboard', key, page: getUrl(), opts: optsOf(opts) });
        if (world.url_during_keyboard) { navigate(world.url_during_keyboard); }
      },
    },
  };
  return self;
}
// world.pages: адреса вкладок (первая считается «основной» и следует за world.url); без него одна вкладка.
const pages = (world.pages || [null]).map((url, index) => makePage(index === 0 && url === null ? () => world.url || 'about:blank' : () => url, index === 0, index));
const targetUrlOf = (page) => {
  const index = pages.indexOf(page);
  if (moved[index]) return moved[index].target;
  return world.targets ? world.targets[index] : page.url();
};
const page = pages[0];
const context = {
  pages: () => (world.no_page ? [] : pages),
  async newPage() { calls.push({ op: 'newPage' }); return page; },
  async newCDPSession(target) {
    if (world.fail && world.fail.cdp_session) throw new Error('Protocol error SECRET-PAGE-TEXT');
    return {
      async send(method, params) {
        cdpCalls.push({ method, params: params ?? null });
        if (method !== 'Target.getTargetInfo') throw new Error(`unexpected ${method}`);
        return { targetInfo: { targetId: `T${pages.indexOf(target)}`, type: 'page', url: targetUrlOf(target) } };
      },
      async detach() { cdpCalls.push({ method: 'detach' }); },
    };
  },
};
const chromium = {
  async connectOverCDP(url) {
    calls.push({ op: 'connect', url });
    if (world.cdp_down) throw new Error('connect ECONNREFUSED SECRET-PAGE-TEXT');
    if (world.unhandled_on_connect) { Promise.reject(new Error('SECRET-PAGE-TEXT')); await sleep(30); }
    return { contexts: () => [context], async close() { calls.push({ op: 'disconnect' }); } };
  },
};

let done;
const exited = new Promise((resolve) => { done = resolve; });
await Promise.race([main({
  chromium: world.no_playwright ? null : chromium,
  readInput: async () => (raw !== undefined ? raw : JSON.stringify(envelope)),
  write: (line) => { out += line; },
  exit: (code) => { exitCodes.push(code); done(); },
}), exited]);
process.stdout.write(JSON.stringify({ output: out, calls, exit_codes: exitCodes, cdp: cdpCalls }));
process.exit(0);
