// Исполнитель одного шага процедуры (docs/contracts.md, раздел 14). Запускается лаунчером под uid 1001:
//   node /usr/local/libexec/procedure-step.mjs   (конверт JSON на stdin, одна строка JSON результата на stdout)
//
// Вход (stdin, один объект):
//   {"dry_run": bool, "deadline_ms": 1000..180000,
//    "payload": {"step": {"action","target","value","precondition","expect"}, "settle_ms"?: 0..5000, "check_expect"?: bool}}
// dry_run=true: ничего не делает на странице, только читает: адрес, видимость precondition.visible, число совпадений цели
//   и её подпись (роль, имя), при check_expect ещё видимость expect.visible и текста expect.text (без ожидания).
// dry_run=false: те же проверки перед действием, затем действие. Проверку expect (в том числе url_matches) делает ядро
//   повторными dry_run: регулярные выражения считает Python с пределом по времени (procedures.match_url), не этот скрипт.
//
// Сверка страницы в одном вызове (payload.expected = {origin, url, role, name}): скрипт сам находит вкладку с этим адресом,
// сверяет origin и подпись элемента и только потом действует. Нет вкладки с таким адресом, другой origin или другая подпись:
// код `changed`, действия не было. Шаг без expected допустим только для `navigate` (первая страница).
//
// Переход после действия. Click, press, select и fill без секрета, после которых вкладка ушла на другую страницу (в том числе на
// другой origin), это обычный результат: ok=true, acted=true, url новой страницы, navigated=true (и origin_changed=true, если
// сменился origin). Адрес читается заново после действия, сам скрипт новую страницу не ждёт. Проверку адреса (url_forbidden) и
// expect по нему делает ядро. Исключение: fill с payload.secret_input и сменившимся origin это код `changed_after` с acted=true
// (значение могло уйти не туда). Оборванное действие (ошибка Playwright) остаётся acted=null: `action_failed`, при секрете
// `changed_after`. Ожидание есть только в чтении: при settle_ms читающий вызов ждёт загрузки (domcontentloaded) и цели, не дольше
// settle_ms, и отдаёт адрес уже после ожидания.
//
// Безопасность: значение шага (`step.value`, оно же секрет у fill) нигде не печатается и не попадает в результат; текста
// страницы и текстов ошибок Playwright в результате нет, только коды и подпись найденного элемента (роль, имя до 200
// символов). stderr не используется. Подключение: CDP на 127.0.0.1:9222 внутри контейнера, других адресов нет.
// Playwright берётся только по абсолютному пути из образа (RUNTIME_PATHS): ни HOME, ни NODE_PATH, ни каталог под uid 1001
// модуль подменить не могут. Нет файла: код `runtime_missing`. Необработанное исключение и отказ промиса: одна строка JSON
// с кодом `internal_error` и фазой (before_action: acted=false, after_action: acted=null), без текста ошибки, выход 70.
import { createRequire } from 'node:module';
import { createHash } from 'node:crypto';
import { existsSync, realpathSync, writeSync } from 'node:fs';
import { pathToFileURL } from 'node:url';

export const CDP_URL = 'http://127.0.0.1:9222';
export const INPUT_MAX = 512 * 1024;
export const NAME_MAX = 200;
export const URL_MAX = 2048;  // показ адреса в результате
export const EXPECTED_URL_MAX = 8192;  // адрес в expected; полный адрес вкладки опознаётся по url_sha256
export const ACTIONS = ['navigate', 'click', 'fill', 'press', 'select', 'wait', 'assert'];
const ROLE_RE = /^[a-z][a-z0-9-]{0,63}$/;
const SHA256_RE = /^[0-9a-f]{64}$/;
const SELECTOR_MAX = 2000;
// Действие через handle ждёт доступности не дольше этого (мс): страница, ушедшая на другой origin за время ожидания, до действия
// не доживает. Элемент должен быть видим и доступен уже в момент сверки.
export const ACTION_TIMEOUT_MS = 2000;
export const NOT_ACTIONABLE = 'stat_not_actionable';
export const RUNTIME_PATHS = [
  '/usr/lib/node_modules/@playwright/mcp/node_modules/playwright-core/index.js',
  '/usr/lib/node_modules/playwright-core/index.js',
  '/usr/local/lib/node_modules/@playwright/mcp/node_modules/playwright-core/index.js',
  '/usr/local/lib/node_modules/playwright-core/index.js',
];
// Селектор только CSS: префиксы движков Playwright (xpath=, text=, css=, id=, data-testid=, internal:control=...), цепочки
// `>>`, xpath (`//`, `..`, ведущий `/`) и текст в кавычках в CSS не входят.
const SELECTOR_ENGINE_RE = /^\s*(?:[a-z][a-z0-9_-]*\s*=|internal:)/i;

class StepError extends Error {
  constructor(code, acted = false) {
    super(code);
    this.code = code;
    this.acted = acted;
  }
}

const isObject = (value) => value !== null && typeof value === 'object' && !Array.isArray(value);
const clip = (text, max) => String(text ?? '').replace(/\s+/g, ' ').trim().slice(0, max);

// Каркас селектора: без строк в кавычках, без содержимого [..] и без экранированных символов (`md\:flex`). Цепочки `>>`, xpath
// (`//`, `..`) и двоеточие вне скобок (псевдокласс или псевдоэлемент: :has-text, :text, :visible, :light, :hover, ::before) ищутся
// только в каркасе, поэтому `a[href^="https://x"]` и `input[value=".."]` остаются обычным CSS.
function selectorSkeleton(selector) {
  let out = '';
  let quote = null;
  let depth = 0;
  for (let i = 0; i < selector.length; i += 1) {
    const ch = selector[i];
    if (ch === '\\') { i += 1; out += 'x'; continue; }
    if (quote) { if (ch === quote) quote = null; continue; }
    if (ch === '"' || ch === "'") { quote = ch; continue; }
    if (ch === '[') depth += 1;
    else if (ch === ']') { if (depth === 0) return null; depth -= 1; }
    if (depth === 0 || ch === '[' || ch === ']') out += ch;
  }
  return quote === null && depth === 0 ? out : null;  // незакрытая кавычка или скобка: селектор не разобрать, он не принимается
}

export function cssSelectorOk(selector) {
  if (typeof selector !== 'string' || selector.trim() === '' || selector.length > SELECTOR_MAX) return false;
  const head = selector.trimStart();
  if (head.startsWith('/') || head.startsWith('"') || head.startsWith("'") || head.startsWith('`')) return false;
  if (SELECTOR_ENGINE_RE.test(selector)) return false;
  const skeleton = selectorSkeleton(selector);
  return skeleton !== null && !skeleton.includes('>>') && !skeleton.includes('//') && !skeleton.includes('..') && !skeleton.includes(':');
}

// Абсолютный путь к playwright-core из образа (root, rootfs read-only). Никакого поиска по HOME, NODE_PATH и cwd. root: каталог,
// вне которого файл (и его realpath, то есть симлинк) не принимается; параметры нужны только тестам.
export function loadPlaywright(paths = RUNTIME_PATHS, { load = createRequire(import.meta.url), root = '/usr/' } = {}) {
  for (const file of paths) {
    try {
      if (!file.startsWith(root) || !existsSync(file) || !realpathSync(file).startsWith(root)) continue;
      const chromium = load(file).chromium;
      if (chromium) return chromium;
    } catch {
      // следующий путь
    }
  }
  return null;
}

// Origin страницы: схема://хост[:порт] (punycode, порт по умолчанию опущен). Не http(s): null.
export function originOf(url) {
  try {
    const parsed = new URL(url);
    return parsed.protocol === 'http:' || parsed.protocol === 'https:' ? `${parsed.protocol}//${parsed.host}` : null;
  } catch {
    return null;
  }
}

// --- разбор входа --------------------------------------------------------------------------------------------------

function checkTarget(target, field) {
  if (target === null || target === undefined) return null;
  if (!isObject(target)) throw new StepError('invalid_input');
  const keys = Object.keys(target);
  if (typeof target.selector === 'string' && keys.length === 1) {
    if (!cssSelectorOk(target.selector)) throw new StepError('invalid_input');
    return { selector: target.selector };
  }
  if (typeof target.role === 'string' && ROLE_RE.test(target.role) && typeof target.name === 'string'
      && keys.every((key) => key === 'role' || key === 'name')) return { role: target.role, name: target.name };
  if (field === 'navigate' && typeof target.url === 'string' && keys.length === 1) return { url: target.url };
  throw new StepError('invalid_input');
}

function checkCondition(raw, keys) {
  if (raw === null || raw === undefined) return null;
  if (!isObject(raw) || Object.keys(raw).some((key) => !keys.includes(key))) throw new StepError('invalid_input');
  const out = {};
  if (raw.visible !== undefined && raw.visible !== null) out.visible = checkTarget(raw.visible, 'visible');
  if (raw.text !== undefined && raw.text !== null) {
    if (typeof raw.text !== 'string' || raw.text === '') throw new StepError('invalid_input');
    out.text = raw.text;
  }
  return out;
}

function parseExpected(raw) {
  if (raw === undefined || raw === null) return null;
  if (!isObject(raw) || Object.keys(raw).some((key) => !['origin', 'url', 'url_sha256', 'role', 'name'].includes(key))) throw new StepError('invalid_input');
  const { origin = null, url, url_sha256: urlSha = null, role = null, name = null } = raw;
  if (typeof url !== 'string' || url === '' || url.length > EXPECTED_URL_MAX) throw new StepError('invalid_input');
  if (urlSha !== null && !(typeof urlSha === 'string' && SHA256_RE.test(urlSha))) throw new StepError('invalid_input');
  if (origin !== null && (typeof origin !== 'string' || origin.length > URL_MAX)) throw new StepError('invalid_input');
  if (role !== null && !(typeof role === 'string' && ROLE_RE.test(role))) throw new StepError('invalid_input');
  if (name !== null && !(typeof name === 'string' && name.length <= NAME_MAX)) throw new StepError('invalid_input');
  return { origin, url, urlSha, role, name };
}

export function parseInput(text) {
  let envelope;
  try {
    envelope = JSON.parse(text);
  } catch {
    throw new StepError('invalid_input');
  }
  if (!isObject(envelope) || typeof envelope.dry_run !== 'boolean') throw new StepError('invalid_input');
  const deadline = envelope.deadline_ms;
  if (!Number.isFinite(deadline) || deadline < 1000 || deadline > 180000) throw new StepError('invalid_input');
  const payload = envelope.payload;
  if (!isObject(payload) || !isObject(payload.step)) throw new StepError('invalid_input');
  const raw = payload.step;
  if (!ACTIONS.includes(raw.action)) throw new StepError('invalid_input');
  const settle = payload.settle_ms === undefined ? 0 : payload.settle_ms;
  if (!Number.isInteger(settle) || settle < 0 || settle > 5000) throw new StepError('invalid_input');
  if (raw.value !== undefined && raw.value !== null && typeof raw.value !== 'string') throw new StepError('invalid_input');
  const step = {
    action: raw.action,
    target: checkTarget(raw.target, raw.action),
    value: raw.value ?? null,
    precondition: checkCondition(raw.precondition, ['visible', 'url_matches']),
    expect: checkCondition(raw.expect, ['visible', 'text', 'url_matches', 'timeout_ms']),
  };
  const expected = parseExpected(payload.expected);
  if (payload.secret_input !== undefined && typeof payload.secret_input !== 'boolean') throw new StepError('invalid_input');
  // Действие без сверки страницы допустимо только у navigate (первый шаг: страницы может ещё не быть).
  if (!envelope.dry_run && step.action !== 'navigate' && !expected) throw new StepError('invalid_input');
  const needsTarget = ['click', 'fill', 'select', 'assert'].includes(step.action);
  if (needsTarget && (!step.target || step.target.url)) throw new StepError('invalid_input');
  if (step.action === 'navigate' && !(step.target && step.target.url)) throw new StepError('invalid_input');
  // Чтение (dry_run) значения не требует: ядро секрет в такой вызов не кладёт.
  if (!envelope.dry_run && ['fill', 'select', 'press'].includes(step.action) && (step.value === null || step.value === '')) {
    throw new StepError('invalid_input');
  }
  return { dryRun: envelope.dry_run, deadlineMs: deadline, settleMs: settle, checkExpect: payload.check_expect === true, step,
           expected, secretInput: payload.secret_input === true };
}

// --- страница ------------------------------------------------------------------------------------------------------

function locate(page, target) {
  if (target.selector) return page.locator(`css=${target.selector}`);
  return target.name === '' ? page.getByRole(target.role) : page.getByRole(target.role, { name: target.name, exact: true });
}

async function anyVisible(locator, count) {
  for (let i = 0; i < Math.min(count, 20); i += 1) {
    if (await locator.nth(i).isVisible()) return true;
  }
  return false;
}

async function targetVisible(page, target) {
  const locator = locate(page, target);
  const count = await locator.count();
  return count > 0 && anyVisible(locator, count);
}

async function textVisible(page, text) {
  const hit = page.getByText(text);
  const count = await hit.count();
  return count > 0 && anyVisible(hit, count);
}

// Подпись найденного элемента: для цели {role, name} это она сама (поиск точный), для селектора её читает aria-снимок
// элемента, первая строка вида `- button "Оплатить"`. Не разобралось: роль и имя null.
async function liveLabel(locator, target, timeoutMs) {
  if (!target.selector) return { role: target.role, name: clip(target.name, NAME_MAX) };
  try {
    const snapshot = await locator.first().ariaSnapshot({ timeout: Math.max(200, Math.min(timeoutMs, 1500)) });
    const line = String(snapshot).split('\n', 1)[0];
    const match = /^\s*-\s+([A-Za-z][A-Za-z0-9-]*)(?:\s+"((?:[^"\\]|\\.)*)")?/.exec(line);
    if (!match) return { role: null, name: null };
    const name = match[2] === undefined ? '' : match[2].replace(/\\(["\\])/g, '$1');
    return { role: match[1].toLowerCase(), name: clip(name, NAME_MAX) };
  } catch {
    return { role: null, name: null };
  }
}

// Подписка на переходы главного фрейма на время действия: любой переход виден по `navigated`, уход на другой origin по `away`.
function watchNavigation(page, expected) {
  const state = { away: false, navigated: false };
  const listener = (frame) => {
    if (frame !== page.mainFrame()) return;
    state.navigated = true;
    if (expected.origin !== originOf(frame.url())) state.away = true;
  };
  page.on('framenavigated', listener);
  return {
    state,
    stop() {
      try {
        page.off('framenavigated', listener);
      } catch {
        // браузер уже закрыт
      }
    },
  };
}

const pageMoved = (page, expected, watch) => watch.state.away || expected.origin !== originOf(page.url());

// Сверка handle непосредственно перед действием: страница на том же origin, элемент в главном фрейме, не отцеплен, виден и доступен
// (для fill ещё редактируем; для assert достаточно видимости). Автоожидания нет: недоступный элемент это `stat_not_actionable`.
async function guardHandle(handle, page, expected, watch, action) {
  if (pageMoved(page, expected, watch)) throw new StepError('changed');
  if ((await handle.ownerFrame()) !== page.mainFrame()) throw new StepError('changed');
  if (!(await handle.evaluate((el) => el.isConnected))) throw new StepError('changed');
  if (!(await handle.isVisible())) throw new StepError(NOT_ACTIONABLE);
  if (action !== 'assert' && !(await handle.isEnabled())) throw new StepError(NOT_ACTIONABLE);
  if (action === 'fill' && !(await handle.isEditable())) throw new StepError(NOT_ACTIONABLE);
  if (pageMoved(page, expected, watch)) throw new StepError('changed');  // последняя сверка после всех ожиданий: дальше сразу действие
}

// Нажатие клавиши без цели: у page.keyboard.press своего таймаута нет.
async function pressKey(page, key, timeoutMs) {
  let timer;
  try {
    await Promise.race([page.keyboard.press(key),
      new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('timeout')), timeoutMs); })]);
  } finally {
    clearTimeout(timer);
  }
}

export const urlSha256 = (url) => createHash('sha256').update(String(url), 'utf8').digest('hex');

const NEW_TAB_URLS = new Set(['chrome://newtab/', 'chrome://new-tab-page/']);
const BLANK = 'about:blank';
const CDP_READ_MS = 1500;  // чтение адреса цели одной вкладки (сессия, запрос, detach)

// Адрес ЦЕЛИ CDP (targetInfo.url). Вкладкой считается обычная страница, новая вкладка или about:blank. Служебные поверхности
// Chromium (chrome://omnibox-popup… и подобные), расширения, devtools и chrome-untrusted:// не вкладки. chrome-error:// как адрес
// цели не бывает у настоящей вкладки (у неё в цели адрес того, что не загрузилось), поэтому и он не вкладка.
export function isUserTab(url) {
  if (typeof url !== 'string') return false;
  if (url.startsWith('devtools://') || url.startsWith('chrome-extension://') || url.startsWith('chrome-untrusted://')
      || url.startsWith('chrome-error://')) return false;
  if (url.startsWith('chrome://')) return NEW_TAB_URLS.has(url);
  return true;
}

// Адрес цели вкладки одним значением. page.url() для этого не годится: политика Chromium закрывает chrome://*, и у служебных
// страниц и заблокированной новой вкладки адрес фрейма один и тот же (chrome-error://chromewebdata/), а адреса целей разные.
// Цель читается через CDP-сессию самой страницы (Target.getTargetInfo без targetId отвечает про цель сессии); сессия закрывается.
// Не прочиталось: адрес фрейма (chrome-error:// вкладкой тогда не считается, см. isUserTab).
async function targetUrl(context, page) {
  let session = null;
  let timer;
  try {
    const read = (async () => {
      session = await context.newCDPSession(page);
      const info = await session.send('Target.getTargetInfo');
      return info && info.targetInfo && typeof info.targetInfo.url === 'string' ? info.targetInfo.url : null;
    })();
    read.catch(() => {});
    const url = await Promise.race([read, new Promise((resolve) => { timer = setTimeout(() => resolve(null), CDP_READ_MS); })]);
    if (url !== null) return url;
  } catch {
    // адрес фрейма
  } finally {
    clearTimeout(timer);
    if (session) await session.detach().catch(() => {});
  }
  return page.url();
}

// Вкладка: страница, адрес для сверки и показа (одно значение: из него же хэш), признак «страница ошибки». Заблокированная политикой
// новая вкладка выглядит как about:blank. Страница ошибки у настоящей вкладки (фрейм chrome-error://, цель обычная) это не пустая
// вкладка, а провал загрузки; у новой вкладки chrome-error:// штатный, её провалом не считают.
async function tabOf(context, page) {
  const raw = await targetUrl(context, page);
  if (!isUserTab(raw)) return null;
  const blank = NEW_TAB_URLS.has(raw);
  return { page, url: blank ? BLANK : raw, errored: !blank && page.url().startsWith('chrome-error://') };
}

// Вкладка с ожидаемым адресом: по SHA-256 полного адреса, если он передан, иначе по точному совпадению адреса. Две вкладки с
// одним адресом: `ambiguous` (какая из них одобрена, не определить). Без expected (чтение) первая вкладка, но и там двойник
// её адреса это `ambiguous`: действие по такой странице владелец не одобряет. Служебные поверхности Chromium (их Chromium отдаёт
// по CDP как «страницы») отсеиваются: навигация в такую «страницу» отвечает успехом, а видимая вкладка не меняется.
async function pickTab(context, expected = null, checkTwins = true) {
  const tabs = (await Promise.all(context.pages().map((page) => tabOf(context, page)))).filter(Boolean);
  if (expected) {
    const same = expected.urlSha ? (tab) => urlSha256(tab.url) === expected.urlSha : (tab) => tab.url === expected.url;
    const hits = tabs.filter(same);
    if (hits.length > 1) throw new StepError('ambiguous');
    return hits[0] ?? null;
  }
  if (checkTwins && tabs.length > 1 && tabs.slice(1).some((tab) => tab.url === tabs[0].url)) throw new StepError('ambiguous');
  return tabs.length ? tabs[0] : null;
}

function emptyResult() {
  return { v: 1, ok: false, code: null, acted: false, url: null, url_sha256: null, precondition_visible: null, found: null, expect: null,
           navigated: false, origin_changed: false };
}

// --- шаг -----------------------------------------------------------------------------------------------------------

export async function runStep(input, { chromium, now = () => Date.now(), onState = () => {} }) {
  const result = emptyResult();
  const started = now();
  const remaining = () => Math.max(100, input.deadlineMs - (now() - started));
  const { step } = input;
  let browser = null;
  let watch = null;
  try {
    if (!chromium) throw new StepError('runtime_missing');
    if (step.action === 'navigate' && !/^(https?:\/\/[^\s\\]+|about:blank)$/.test(step.target.url)) throw new StepError('url_forbidden');
    try {
      browser = await chromium.connectOverCDP(CDP_URL, { timeout: Math.min(5000, remaining()) });
    } catch {
      throw new StepError('cdp_unavailable');
    }
    const context = browser.contexts()[0];
    if (!context) throw new StepError('no_page');
    const { expected } = input;
    let tab = await pickTab(context, expected, step.action !== 'navigate');
    if (!tab) {
      if (expected && await pickTab(context, null, false)) throw new StepError('changed');  // вкладки с ожидаемым адресом нет: страница сменилась
      if (step.action !== 'navigate' || input.dryRun) throw new StepError('no_page');
      const opened = await context.newPage();
      tab = (await tabOf(context, opened)) ?? { page: opened, url: BLANK, errored: false };
    }
    const { page } = tab;
    // Адрес и хэш результата из одного значения, прочитанного один раз (показ обрезан, хэш считан по полному адресу).
    result.url = clip(tab.url, URL_MAX) || null;
    result.url_sha256 = urlSha256(tab.url);
    if (expected && expected.origin !== originOf(tab.url)) throw new StepError('changed');
    // Страница ошибки настоящей вкладки: читать и действовать на ней нечего (navigate её как раз и покидает).
    if (tab.errored && step.action !== 'navigate') throw new StepError('page_error');

    const target = step.action === 'navigate' ? null : step.target;
    const locator = target ? locate(page, target) : null;
    // Действие с проверкой страницы (всё, что меняет страницу, кроме navigate и wait): подписка на переходы до первой сверки.
    const guarded = !input.dryRun && expected !== null && ['click', 'fill', 'select', 'press', 'assert'].includes(step.action);
    if (guarded) {
      watch = watchNavigation(page, expected);
      if (pageMoved(page, expected, watch)) throw new StepError('changed');
    }
    let handle = null;
    // Ожидание только в чтении (settle_ms от ядра, не больше 5000): загрузка страницы и появление цели с общим пределом. Действие
    // само ничего не ждёт. После ожидания адрес читается заново: вкладка могла уйти на новую страницу, её адрес и надо вернуть.
    const settleStart = now();
    const settleLeft = () => Math.max(1, Math.min(input.settleMs - (now() - settleStart), remaining()));
    if (input.dryRun && input.settleMs > 0 && typeof page.waitForLoadState === 'function') {
      await page.waitForLoadState('domcontentloaded', { timeout: settleLeft() }).catch(() => {});
    }
    if (locator && input.settleMs > 0) {
      await locator.first().waitFor({ state: 'attached', timeout: settleLeft() }).catch(() => {});
    }
    if (input.dryRun && input.settleMs > 0) {
      const fresh = await tabOf(context, page);
      if (fresh) {
        tab = fresh;
        result.url = clip(tab.url, URL_MAX) || null;
        result.url_sha256 = urlSha256(tab.url);
        if (tab.errored && step.action !== 'navigate') throw new StepError('page_error');
      }
    }
    const visiblePre = step.precondition && step.precondition.visible;
    if (visiblePre) {
      result.precondition_visible = await targetVisible(page, visiblePre);
      if (!result.precondition_visible) throw new StepError('precondition_failed');
    }
    if (locator) {
      const count = await locator.count();
      result.found = { count, role: null, name: null };
      if (count === 0) throw new StepError('element_not_found');
      if (count > 1) throw new StepError('element_ambiguous');
      if (guarded) {
        // Действовать можно только через этот handle (он отцепляется при навигации), повторный поиск по локатору исключён.
        try {
          handle = await locator.first().elementHandle({ timeout: Math.min(500, remaining()) });
        } catch {
          throw new StepError('changed');  // элемент пропал между подсчётом и выдачей handle: страница сменилась
        }
      }
      Object.assign(result.found, await liveLabel(locator, target, remaining()));
      // Подпись элемента сверяется в этом же вызове, перед действием (в чтении expected не приходит).
      if (expected && expected.role !== null && (result.found.role !== expected.role || result.found.name !== expected.name)) {
        throw new StepError('changed');
      }
    }
    if (input.dryRun) {
      if (input.checkExpect && step.expect) {
        result.expect = {
          visible: step.expect.visible ? await targetVisible(page, step.expect.visible) : null,
          text: step.expect.text ? await textVisible(page, step.expect.text) : null,
        };
      }
      result.ok = true;
      return result;
    }

    if (guarded) {
      if (handle) await guardHandle(handle, page, expected, watch, step.action);
      else if (pageMoved(page, expected, watch)) throw new StepError('changed');  // press без цели: адрес сверен перед нажатием
    }

    // Действие. С этой строки исход при ошибке неизвестен (`acted: null`), пока Playwright не вернул управление. Действие идёт
    // только через handle, с коротким таймаутом и без force: ожидания доступности (и перехода на чужой origin за это время) нет.
    onState('acting');
    result.acted = null;
    const timeout = Math.min(ACTION_TIMEOUT_MS, remaining());
    const options = { timeout, noWaitAfter: true };
    try {
      switch (step.action) {
        case 'navigate': await page.goto(step.target.url, { waitUntil: 'domcontentloaded', timeout: remaining() }); break;
        case 'click': await handle.click(options); break;
        case 'fill': await handle.fill(step.value, options); break;
        case 'select': await handle.selectOption(step.value, options); break;
        case 'press':
          if (handle) await handle.press(step.value, options); else await pressKey(page, step.value, timeout);
          break;
        case 'wait': {
          const wait = remaining();
          if (locator) await locator.waitFor({ state: 'visible', timeout: wait });
          else await new Promise((resolve) => setTimeout(resolve, Math.min(Number(step.value) || 0, wait)));
          break;
        }
        case 'assert': {
          if (step.value) {
            const text = await handle.innerText({ timeout });
            if (!text.includes(step.value)) throw new StepError('assert_failed');  // только чтение: действия не было
          }
          break;
        }
        default: throw new StepError('invalid_input');
      }
    } catch (err) {
      if (err instanceof StepError) throw err;
      // Действие оборвалось, а страница ушла на другой origin. Исход неизвестен; при вводе секрета причина названа (`changed_after`).
      if (input.secretInput && guarded && pageMoved(page, expected, watch)) throw new StepError('changed_after', null);
      throw new StepError('action_failed', null);
    }
    result.acted = true;
    // После действия адрес читается заново одним значением: из него и показ, и хэш.
    const after = await tabOf(context, page);
    const shown = after ? after.url : page.url();
    result.url = clip(shown, URL_MAX) || null;
    result.url_sha256 = urlSha256(shown);
    // Переход после действия: страница другая (событие главного фрейма или другой адрес вкладки), origin мог смениться.
    if (guarded) {
      result.origin_changed = pageMoved(page, expected, watch) || expected.origin !== originOf(shown);
      result.navigated = watch.state.navigated || shown !== tab.url || result.origin_changed;
    }
    // Ввод секрета и другой origin: значение могло уйти не туда. Остальные действия с переходом это успех шага.
    if (input.secretInput && result.origin_changed) throw new StepError('changed_after', true);
    // Действие выполнено, а вкладка оказалась на странице ошибки: это не успех (navigate с safe_to_retry ядро повторит).
    if (after && after.errored) throw new StepError('page_error', true);
    result.ok = true;
    return result;
  } catch (err) {
    result.ok = false;
    result.code = err instanceof StepError ? err.code : 'action_failed';
    result.acted = err instanceof StepError ? err.acted : null;
    return result;
  } finally {
    if (watch) watch.stop();
    if (browser) {
      await Promise.race([browser.close().catch(() => {}), new Promise((resolve) => setTimeout(resolve, 2000))]);
    }
  }
}

// --- вход и выход --------------------------------------------------------------------------------------------------

async function readStdin() {
  const chunks = [];
  let size = 0;
  for await (const chunk of process.stdin) {
    size += chunk.length;
    if (size > INPUT_MAX) throw new StepError('invalid_input');
    chunks.push(chunk);
  }
  return Buffer.concat(chunks).toString('utf8');
}

export async function main({ chromium = undefined, readInput = readStdin, write = (line) => writeSync(1, line), exit = process.exit,
  proc = process } = {}) {
  let emitted = false;
  const emit = (result) => {
    if (emitted) return;  // одна строка результата на запуск: сработавший обработчик сбоя или срок не дописывают вторую
    emitted = true;
    write(`${JSON.stringify(result)}\n`);
  };
  // Фаза нужна ядру: до действия сбой значит «ничего не сделано» (acted=false), после начала действия исход неизвестен.
  let phase = 'before_action';
  const crash = () => {
    const result = emptyResult();
    result.code = 'internal_error';
    result.phase = phase;
    result.acted = phase === 'after_action' ? null : false;
    try {
      emit(result);
    } finally {
      exit(70);  // текст ошибки не печатается нигде: в нём бывают значения шага
    }
  };
  proc.on('uncaughtException', crash);
  proc.on('unhandledRejection', crash);
  let input;
  try {
    input = parseInput(await readInput());
  } catch (err) {
    const result = emptyResult();
    result.code = err instanceof StepError ? err.code : 'invalid_input';
    emit(result);
    return exit(0);
  }
  // Свой срок короче жёсткого предела лаунчера: на выходе по сроку ядро получает код `timeout`, а не обрыв.
  const timer = setTimeout(() => {
    const result = emptyResult();
    result.code = 'timeout';
    result.acted = phase === 'after_action' ? null : false;
    emit(result);
    exit(0);
  }, input.deadlineMs);
  const result = await runStep(input, { chromium: chromium === undefined ? loadPlaywright() : chromium, onState: (s) => { if (s === 'acting') phase = 'after_action'; } });
  clearTimeout(timer);
  emit(result);
  return exit(0);
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main();
}
