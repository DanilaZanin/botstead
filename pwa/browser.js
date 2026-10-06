// browser.js: экран «Браузер бота» (docs/contracts.md §13): живой экран, перехват управления, скрытый ввод пароля, лента шагов.
// Состояние управления (bot, human, returning) приходит с сервера: GET /browser при входе, события browser_control из треда бота
// и опрос раз в несколько секунд. При расхождении побеждает сервер. Ввод в экран включён только в состоянии human.
// Пароль уходит одним запросом secret-input и нигде не хранится: поле стирается при отправке, в события и ленту он не попадает.
import * as api from './api.js';
import { ICONS, esc, icon } from './ui.js';
import { openDialog, confirmBody, wireConfirm, setAlert, setBusy, setFieldError, clearErrors, stateHtml } from './account.js';
import { recreateBotAction } from './providers.js';
import { locale } from './i18n.js';
import { openThreadStream } from './ws.js';
import { openScreen } from './screen.js';

const STEP_LIMIT = 200;
const RETURN_LOCK_MS = 600;
const LAST_STEPS = 3;
const POLL_MS = api.MOCK ? 1500 : 5000;
const ESCAPE_KEY = 'Shift+Esc';
const SECRET_NAME = /^[A-Za-z][A-Za-z0-9_-]{0,63}$/;
const RISK_LABEL = { pay: 'ОПЛАТА', send: 'ОТПРАВКА', delete: 'УДАЛЕНИЕ', login: 'ВХОД', push: 'ПУБЛИКАЦИЯ', exec: 'КОМАНДА', other: 'ДЕЙСТВИЕ' };
const IRREVERSIBLE = ['pay', 'send', 'delete', 'push'];
const SECRET_KEYS = /^(value|text|password|secret|pass|token)$/i;

const CONTROL = {
  bot: { who: 'Управляет бот', title: 'Управляет бот', sub: 'Экран только для просмотра. Чтобы вмешаться, перехватите управление.', chip: 'Только просмотр' },
  human: { who: 'Управляете вы', title: 'Управляете вы', sub: 'Задача бота остановлена, шаг на паузе. Мышь и клавиатура идут в браузер бота.', chip: 'Ввод включён' },
  returning: { who: 'Возврат управления', title: 'Возврат боту', sub: 'Ввод выключен: бот заново читает страницу. Прежние подтверждения сброшены.', chip: 'Ввод выключен' },
};

const KEYBOARD_ICON = icon('<rect x="2.5" y="6" width="19" height="12" rx="2.5"></rect><path d="M6 10h.01M10 10h.01M14 10h.01M18 10h.01M7 14h10"></path>', 20, 2.2);
// Экраны без картинки, где управление у человека: бот на паузе, поэтому «Вернуть боту» нужна и там.
const HUMAN_RETURN_SCREENS = ['lost', 'down', 'busy', 'unavailable', 'client'];
const PAUSED_NOTE = 'Бот на паузе, пока управление у вас';

export const controlTitle = (state) => (CONTROL[state] ? CONTROL[state].title : 'Состояние экрана неизвестно');

const NAME_HINT = 'Секрет сохранится под этим именем. Латиница, цифры, _ и -, начинается с буквы, до 64 символов.';
const GUARANTEE = 'Пароль вводится напрямую в браузер бота. Модель не получает экран и не действует, пока управляете вы.';

const $ = (selector, root = document) => root.querySelector(selector);
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// Браузер есть у ботов на Claude с компьютером на сервере (раздел 13): для Codex, Gemini и Mac инструменты недоступны.
export function hasBrowser(bot) {
  if (!bot) return false;
  if (api.MOCK && api.MOCK_BROWSER_MODE === 'none') return false;
  return ['claude', 'fake'].includes(bot.provider) && bot.executor !== 'mac';
}

// ---------------------------------------------------------------------------
// Чистые помощники
// ---------------------------------------------------------------------------
// Длинный адрес режем по середине: начало (сайт) и конец (страница) важнее середины.
export function midTruncate(text, max = 44) {
  const s = String(text ?? '');
  if (s.length <= max) return s;
  const head = Math.ceil((max - 1) * 0.6);
  const tail = Math.floor((max - 1) * 0.4);
  return `${s.slice(0, head)}…${s.slice(s.length - tail)}`;
}
const fmtTokens = (n) => (n >= 1000 ? `${Math.round(n / 1000)}k` : String(n));
const fmtClock = (sec) => `${String(Math.floor(sec / 60)).padStart(2, '0')}:${String(Math.floor(sec % 60)).padStart(2, '0')}`;
const fmtTime = (iso) => {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? '' : d.toLocaleTimeString(locale(), { hour: '2-digit', minute: '2-digit', second: '2-digit' });
};
const hostOf = (url) => {
  try { return new URL(/^[a-z]+:\/\//i.test(url) ? url : `https://${url}`).host; } catch { return String(url || ''); }
};

// Шаг ленты из события browser_step. Ядро уже заменило ввод и адреса вне origin; значение ввода здесь не читаем вовсе.
export function stepModel(ev) {
  const p = ev.payload || {};
  const error = p.result === 'error';
  const model = { seq: ev.seq, time: ev.ts ? fmtTime(ev.ts) : '', error, title: '', target: '', url: '' };
  switch (p.action) {
    case 'navigate': model.title = 'Открыл страницу'; model.url = p.url ? String(p.url) : ''; break;
    case 'click': model.title = 'Нажал'; model.target = p.target ? String(p.target) : 'элемент страницы'; break;
    case 'fill': model.title = 'Заполнил поле'; model.target = 'ввод скрыт ••••••'; break;
    case 'snapshot': model.title = 'Прочитал страницу'; break;
    case 'screenshot': model.title = 'Сделал снимок экрана'; break;
    default: model.title = String(p.action || 'Действие');
  }
  return model;
}

function stepHtml(m) {
  const mark = m.error
    ? `<span class="br-step-mark is-error" aria-hidden="true">${ICONS.closeLg}</span>`
    : `<span class="br-step-mark is-ok" aria-hidden="true">${ICONS.check}</span>`;
  return `<li class="br-step${m.error ? ' is-error' : ''}" data-seq="${esc(m.seq)}">${mark}
    <span class="br-step-body">
      <span class="br-step-title" data-i18n-skip>${esc(m.title)}${m.target ? `<span class="br-step-target">${esc(midTruncate(m.target, 40))}</span>` : ''}</span>
      ${m.url ? `<span class="br-step-url t-log" title="${esc(m.url)}" data-i18n-skip>${esc(midTruncate(m.url, 38))}</span>` : ''}
      <span class="br-step-meta t-footnote">${m.error ? 'Ошибка' : 'Готово'}${m.time ? ` · ${esc(m.time)}` : ''}</span>
    </span></li>`;
}

const STEPS_EMPTY = 'Шагов пока нет. Они появятся, когда бот начнёт работать в браузере.';

// ---------------------------------------------------------------------------
// Разметка
// ---------------------------------------------------------------------------
export function browserBodyHtml({ bot, desktop }) {
  return `<div class="br" id="br" data-screen="connecting" data-control="">
    <div class="br-strip" id="br-strip">
      <div class="br-addr" id="br-addr-group" role="group" aria-label="Адрес страницы, только чтение">${ICONS.lock}<span class="br-addr-text t-log" id="br-addr"></span><span class="br-addr-tag t-caption" id="br-addr-tag">только чтение</span></div>
      <div class="br-tools" id="br-tools">
        <button type="button" class="icon-btn sunken br-tool" data-br="scale" aria-pressed="true" aria-label="Масштаб: по размеру окна">${ICONS.fit}</button>
        <button type="button" class="icon-btn sunken br-tool" data-br="fullscreen" aria-pressed="false" aria-label="Во весь экран">${ICONS.expand}</button>
      </div>
    </div>
    <div class="br-stage-wrap" id="br-stage-wrap">
      <div class="br-stage" id="br-stage" role="application" aria-label="Экран браузера бота ${esc(bot ? bot.name : '')}" aria-describedby="br-kbd-hint" tabindex="0">
        <div class="br-host" id="br-host" data-i18n-skip></div>
        <img class="br-last" id="br-last" alt="" hidden>
        <div class="br-overlay" id="br-overlay" role="status"></div>
      </div>
      <div class="br-who" id="br-who" aria-hidden="true" hidden><span class="br-who-dot"></span><span id="br-who-text"></span></div>
      <div class="br-fs-action" id="br-fs-action" hidden></div>
      <textarea class="br-kbd-input" id="br-kbd-input" rows="1" aria-label="Ввод с клавиатуры в браузер бота" autocomplete="off" autocapitalize="none" autocorrect="off" spellcheck="false" tabindex="-1"></textarea>
      <div class="br-approval" id="br-approval" hidden></div>
    </div>
    <p class="t-footnote br-kbd desktop-only" id="br-kbd-hint" aria-live="polite"></p>
    <div class="br-state" id="br-state" hidden></div>
    <div class="br-panel" id="br-panel">
      <div class="br-flash" id="br-flash" role="status" aria-live="polite"></div>
      <section class="br-control" id="br-control" role="status" aria-live="polite" aria-atomic="true">
        <h2 class="br-control-title" id="br-control-title"></h2>
        <p class="t-footnote br-control-sub" id="br-control-sub"></p>
        <div class="br-chips"><span class="badge badge-sunken" id="br-chip"></span><span class="t-log br-spend" id="br-spend"></span></div>
      </section>
      ${desktop ? '' : `<section class="br-steps-last" aria-labelledby="br-steps-last-title">
        <div class="br-steps-head"><h2 class="br-section-title" id="br-steps-last-title">Последние шаги</h2><button type="button" class="btn btn-ghost br-all" data-br="all-steps">Все шаги</button></div>
        <p class="t-footnote" data-steps-empty>${STEPS_EMPTY}</p>
        <ol class="br-step-list" data-steps-list data-limit="${LAST_STEPS}" aria-label="Последние шаги бота"></ol>
      </section>`}
    </div>
    <div class="sr-only" id="br-announce" role="status" aria-live="polite"></div>
  </div>`;
}

export function browserAsideHtml() {
  return `<section class="br-steps" aria-labelledby="br-steps-title">
    <h2 class="desktop-aside-label" id="br-steps-title">Шаги</h2>
    <p class="t-footnote" data-steps-empty>${STEPS_EMPTY}</p>
    <ol class="br-step-list" data-steps-list data-limit="${STEP_LIMIT}" aria-label="Шаги бота в браузере, новые сверху"></ol>
  </section>`;
}

// ---------------------------------------------------------------------------
// Экран
// ---------------------------------------------------------------------------
export function mountBrowser({ app, bot, botId, setCleanup }) {
  const root = $('#br', app);
  const name = bot ? bot.name : 'бота';
  const st = {
    control: null, screen: 'connecting', fit: true, fullscreen: false, capturing: false,
    steps: [], seen: new Set(), address: '', approvals: [], threadId: null,
    spend: { tokens: 0, seconds: 0, start: null, last: null },
    busy: false, lostReason: '', busyNote: '', busyRetries: 1, auto410: 1, liveSince: 0, returnLock: false,
  };
  let disposed = false;
  let returnLockTimer = null;
  let controller = null;
  let connSeq = 0;
  let stopStream = null;
  let pollTimer = null;
  let dialog = null;
  let deniedText = null;
  let fsViaApi = false;
  let epoch = 0;
  const tabId = Math.random().toString(36).slice(2);
  const channel = 'BroadcastChannel' in window ? new BroadcastChannel('botstead-screen') : null;
  const el = (id) => $(`#${id}`, app);
  const host = el('br-host');
  const stage = el('br-stage');
  const kbd = el('br-kbd-input');

  const announce = (text) => { const live = el('br-announce'); if (live) live.textContent = text; };
  const flash = (title, text = '', kind = 'info') => {
    const box = el('br-flash');
    if (!box) return;
    box.innerHTML = title ? `<div class="banner banner-${kind}"${kind === 'danger' ? ' role="alert"' : ''}><span class="banner-icon">${kind === 'danger' ? ICONS.alert : ICONS.check}</span><span class="banner-text"><span class="banner-title">${esc(title)}</span>${text ? `<span class="banner-sub">${esc(text)}</span>` : ''}</span></div>` : '';
  };

  // ---- состояние управления ----
  function paintControl() {
    const c = CONTROL[st.control];
    root.dataset.control = st.control || '';
    if (c) {
      el('br-control-title').textContent = c.title;
      el('br-control-sub').textContent = c.sub;
      el('br-chip').textContent = c.chip;
    }
    const who = el('br-who');
    who.hidden = !c;
    if (c) el('br-who-text').textContent = c.who;
    // «Только чтение» про адрес верно всегда, но в ручном режиме метка путает рядом с активным вводом.
    const human = st.control === 'human';
    el('br-addr-tag').hidden = human;
    el('br-addr-group').setAttribute('aria-label', human ? 'Адрес страницы' : 'Адрес страницы, только чтение');
    paintKeyboardButton();
    paintActions();
    paintHint();
    stage.classList.toggle('is-human', human);
  }

  // Кнопка «Клавиатура» есть только в ручном режиме; на широком экране с мышью CSS её прячет.
  function paintKeyboardButton() {
    const tools = el('br-tools');
    const have = $('[data-br="keyboard"]', tools);
    if (st.control === 'human' && !have) {
      tools.insertAdjacentHTML('afterbegin', `<button type="button" class="icon-btn sunken br-tool br-kbd-btn" data-br="keyboard" aria-label="Клавиатура" title="Клавиатура">${KEYBOARD_ICON}</button>`);
    } else if (st.control !== 'human' && have) {
      have.remove();
    }
  }

  function applyServerState(s, { own = false } = {}) {
    if (!s || !CONTROL[s.state]) return;
    if (own) epoch += 1;
    const changed = s.state !== st.control;
    st.control = s.state;
    if (changed) {
      paintControl();
      repaintState();
      if (controller) {
        controller.setViewOnly(st.control !== 'human');
        if (st.control !== 'human' && st.capturing) releaseCapture();
      }
    }
  }

  async function refresh() {
    if (disposed || st.busy || st.screen === 'none') return;
    const mine = epoch;
    try {
      const state = await api.getBrowser(botId);
      if (mine === epoch && !st.busy) applyServerState(state);
    } catch (err) {
      if (err && (err.status === 403 || err.status === 404) && st.screen !== 'denied') setScreen('denied');
    }
    refreshApprovals();
  }

  // ---- кнопки действий ----
  // Сразу после перехвата «Вернуть боту» занята: двойной тап по «Перехватить» иначе попадает в неё и тут же возвращает управление.
  const returnButton = (primary = true) => `<button type="button" class="btn ${primary ? 'btn-primary' : 'btn-secondary'}" data-br="return"${st.returnLock ? ' disabled' : ''}>${ICONS.play}Вернуть боту</button>`;
  function unlockReturn() {
    st.returnLock = false;
    clearTimeout(returnLockTimer);
    returnLockTimer = null;
    document.querySelectorAll('[data-br="return"]').forEach((b) => { if (!b.hasAttribute('aria-busy')) b.disabled = false; });
  }

  function paintActions() {
    const bar = el('br-actions');
    if (!bar) return;
    const withStage = st.screen === 'live' || st.screen === 'connecting' || st.screen === 'reconnecting';
    let html = '';
    let main = '';
    if (withStage) {
      if (st.control === 'bot') { html = `<button type="button" class="btn btn-primary" data-br="takeover">${ICONS.handoff}Перехватить</button>`; main = html; }
      else if (st.control === 'human') { html = `<button type="button" class="btn btn-secondary" data-br="secret">${ICONS.key}Пароль скрыто</button>${returnButton()}`; main = returnButton(); }
      else if (st.control === 'returning') { html = `<button type="button" class="btn btn-secondary" data-br="takeover">${ICONS.handoff}Перехватить</button>`; main = html; }
    } else if (st.control === 'human' && HUMAN_RETURN_SCREENS.includes(st.screen)) {
      html = returnButton();
    }
    bar.innerHTML = html;
    bar.classList.toggle('br-actions-2', st.control === 'human' && withStage);
    const outer = bar.closest('.action-bar');
    if (outer) outer.hidden = !html;
    // Во весь экран панель действий закрыта экраном: главное действие дублируется поверх картинки.
    const fs = el('br-fs-action');
    if (fs) {
      fs.innerHTML = st.fullscreen ? main : '';
      fs.hidden = !st.fullscreen || !main;
    }
  }

  function paintHint() {
    const hint = el('br-kbd-hint');
    if (!hint) return;
    if (st.screen !== 'live') { hint.textContent = ''; return; }
    if (st.control !== 'human') hint.textContent = 'Режим просмотра: клавиатура и мышь в браузер бота не передаются.';
    else if (st.capturing) hint.textContent = `Ввод идёт в браузер бота. Выйти из экрана: ${ESCAPE_KEY}.`;
    else hint.textContent = `Чтобы печатать в браузер бота, нажмите Enter или кликните по экрану. Выйти из экрана: ${ESCAPE_KEY}.`;
  }

  // ---- состояния экрана ----
  // В ручном режиме бот стоит: к тексту ошибки добавляем, что задача на паузе (а не «бот мог продолжить работу»).
  const paused = (text) => (st.control === 'human' ? `${text} ${PAUSED_NOTE}.` : text);
  function stateBox(screen) {
    const retry = '<button type="button" class="btn btn-secondary" data-br="reconnect">' + ICONS.retry + 'Подключиться снова</button>';
    switch (screen) {
      case 'down':
        return stateHtml({
          iconHtml: ICONS.cloudOff, kind: 'state-error', title: 'Компьютер бота не запущен',
          text: paused('Экран подключается к запущенному компьютеру бота. Пересоздание запускает его заново: файлы бота и сохранённые входы остаются, открытые вкладки и текущая задача пропадут.'),
          actions: `${retry}<span data-bot-state="error_starting" class="br-recreate"><span class="form-alert" data-recreate-alert aria-live="polite"></span><button type="button" class="btn btn-primary" data-br="recreate" data-bot="${esc(botId)}">${ICONS.retry}Пересоздать компьютер</button></span>`,
        });
      case 'unavailable':
        return stateHtml({
          iconHtml: ICONS.cloudOff, kind: 'state-error', title: 'Экран не открылся',
          text: paused(`Компьютер бота не запущен, либо экран уже открыт в другой вкладке или на другом устройстве. ${st.busyNote || 'Закройте экран там и откройте здесь или пересоздайте компьютер.'}`),
          actions: `<button type="button" class="btn btn-primary" data-br="claim">Открыть здесь</button><span data-bot-state="error_starting" class="br-recreate"><span class="form-alert" data-recreate-alert aria-live="polite"></span><button type="button" class="btn btn-secondary" data-br="recreate" data-bot="${esc(botId)}">${ICONS.retry}Пересоздать компьютер</button></span>`,
        });
      case 'busy':
        return stateHtml({
          iconHtml: ICONS.screen, title: 'Экран открыт на другом устройстве',
          text: paused(`Экран бота показывается в одном месте: в другой вкладке, приложении или на другом устройстве. ${st.busyNote || 'Закройте его там или откройте здесь.'}`),
          actions: '<button type="button" class="btn btn-primary" data-br="claim">Открыть здесь</button>',
        });
      case 'denied':
        return stateHtml({
          iconHtml: ICONS.lock, kind: 'state-error', title: 'Нет доступа к экрану',
          text: deniedText || 'Бот не найден или принадлежит другому пользователю. Вернитесь к списку ботов.',
          actions: '<a class="btn btn-secondary" href="#/">К списку ботов</a>',
        });
      case 'lost':
        return stateHtml({
          iconHtml: ICONS.cloudOff, kind: 'state-error', title: st.lostReason === 'network' ? 'Нет связи с сервером' : 'Связь с экраном потеряна',
          text: st.lostReason === 'network' ? 'Проверьте сеть или VPN.'
            : st.control === 'human' ? `${PAUSED_NOTE}. Подключитесь к экрану снова или верните управление боту.`
              : 'Картинка остановилась. Бот мог продолжить работу, ход задачи виден в ленте шагов.',
          actions: st.lostReason === 'network' ? '<button type="button" class="btn btn-secondary" data-br="reload">' + ICONS.retry + 'Повторить</button>' : retry,
        });
      case 'client':
        return stateHtml({
          iconHtml: ICONS.screen, kind: 'state-error', title: 'Показ экрана не загрузился',
          text: paused('Файлы клиента экрана не найдены или не загрузились. Проверьте сеть и обновите страницу.'),
          actions: '<button type="button" class="btn btn-secondary" data-br="reload">' + ICONS.retry + 'Обновить</button>',
        });
      case 'none':
        return stateHtml({
          iconHtml: ICONS.browser, title: 'У этого бота нет браузера',
          text: 'Браузер есть у ботов на Claude с компьютером на сервере. Этот бот работает на другой модели или на Mac.',
          actions: `<a class="btn btn-secondary" href="#/bots/${esc(botId)}">Настройки бота</a>`,
        });
      default: return '';
    }
  }

  // Текст карточки зависит от того, у кого управление: при смене управления перерисовываем её, пока она на экране.
  function repaintState() {
    const box = el('br-state');
    if (box.hidden || !stateBox(st.screen)) return;
    box.innerHTML = stateBox(st.screen);
  }

  function setScreen(screen, note) {
    st.screen = screen;
    if (note !== undefined) st.busyNote = note;
    root.dataset.screen = screen;
    const live = screen === 'live';
    const showStage = ['connecting', 'reconnecting', 'live'].includes(screen);
    const box = el('br-state');
    el('br-stage-wrap').hidden = !showStage;
    el('br-strip').hidden = !showStage;
    box.hidden = showStage;
    box.innerHTML = showStage ? '' : stateBox(screen);
    const overlay = el('br-overlay');
    overlay.hidden = live;
    overlay.innerHTML = screen === 'reconnecting'
      ? `<span class="spin">${ICONS.spinner}</span>Переподключаю экран`
      : screen === 'connecting' ? `<span class="spin">${ICONS.spinner}</span>Подключаемся к экрану` : '';
    stage.setAttribute('aria-busy', String(!live && showStage));
    if (!live) { st.capturing = false; stage.classList.remove('is-capturing'); }
    if (!showStage) {
      const approval = el('br-approval');
      approval.hidden = true;
      approval.innerHTML = '';
      if (st.fullscreen) toggleFullscreen(false);
    }
    paintActions();
    paintHint();
    paintApproval();
  }

  // ---- подключение экрана ----
  async function connect({ keepFrame = false } = {}) {
    const my = ++connSeq;
    if (controller) { try { controller.close(); } catch { /* уже закрыт */ } controller = null; }
    host.replaceChildren();
    if (!keepFrame) { const last = el('br-last'); last.hidden = true; last.removeAttribute('src'); }
    setScreen(keepFrame ? 'reconnecting' : 'connecting');
    let next;
    try {
      next = await openScreen({
        host, botId, viewOnly: st.control !== 'human', fit: st.fit,
        onConnect: () => onScreenConnect(my),
        onClose: (info) => onScreenClose(my, info),
      });
    } catch {
      if (my === connSeq && !disposed) setScreen('client');
      return;
    }
    if (my !== connSeq || disposed) { next.close(); return; }
    controller = next;
    host.classList.toggle('is-actual', !st.fit);
  }

  function onScreenConnect(my) {
    if (my !== connSeq || disposed) return;
    const last = el('br-last');
    last.hidden = true;
    last.removeAttribute('src');
    st.liveSince = Date.now();
    st.busyRetries = 1;
    st.busyNote = '';
    setScreen('live');
    controller?.setViewOnly(st.control !== 'human');
    announce('Экран подключён.');
  }

  async function onScreenClose(my, { code, opened }) {
    if (my !== connSeq || disposed) return;
    const frame = controller && controller.lastFrame ? controller.lastFrame() : null;
    if (code === 4410) {
      // Экран сняли на сервере (перехват, перезапуск потока): один раз переподключаемся, последний кадр остаётся на месте.
      if (Date.now() - st.liveSince > 10000) st.auto410 = 1;
      if (st.auto410 > 0) {
        st.auto410 -= 1;
        if (frame) { const last = el('br-last'); last.src = frame; last.hidden = false; }
        setScreen('reconnecting');
        await sleep(500);
        if (my === connSeq && !disposed) connect({ keepFrame: !!frame });
        return;
      }
      st.lostReason = '';
      setScreen('lost');
      return;
    }
    if (code === 4401) {
      try { await api.authMe(); } catch (err) { if (err && err.status === 401) { window.dispatchEvent(new CustomEvent('bothub-unauthorized')); return; } }
      if (my !== connSeq || disposed) return;
      deniedText = 'Сессия закрыта или у пользователя нет доступа к этому боту.';
      setScreen('denied');
      return;
    }
    if (code === 4404) { deniedText = null; setScreen('denied'); return; }
    if (code === 4409) {
      if (st.busyRetries > 0) {
        // Прежний сокет мог ещё не закрыться на сервере (перерисовка, переход): один раз пробуем снова.
        st.busyRetries -= 1;
        await sleep(800);
        if (my === connSeq && !disposed) connect();
        return;
      }
      setScreen('busy');
      return;
    }
    if (code === 1013) { setScreen('down'); return; }
    if (!opened) {
      // Отказ до рукопожатия браузер показывает как 1006 без причины: проверяем доступ отдельным запросом.
      try { await api.getBrowser(botId); } catch (err) {
        if (err && (err.status === 403 || err.status === 404)) { deniedText = null; setScreen('denied'); return; }
      }
      if (my === connSeq && !disposed) setScreen('unavailable');
      return;
    }
    st.lostReason = '';
    setScreen('lost');
  }

  function claimHere() {
    // Другие вкладки этого браузера отпускают экран, если он у них открыт; чужие устройства закрываются только вручную.
    if (channel) channel.postMessage({ type: 'claim', bot: botId, from: tabId });
    st.busyRetries = 0;
    st.busyNote = '';
    setScreen('connecting');
    sleep(600).then(() => { if (!disposed) connect().then(() => { st.busyNote = 'Экран по-прежнему не открывается. Закройте его на другом устройстве и повторите.'; }); });
  }
  if (channel) {
    channel.onmessage = (e) => {
      const m = e.data;
      if (!m || m.type !== 'claim' || m.bot !== botId || m.from === tabId || disposed) return;
      if (!['live', 'connecting', 'reconnecting'].includes(st.screen)) return;
      connSeq += 1;
      if (controller) { try { controller.close(); } catch { /* уже закрыт */ } controller = null; }
      host.replaceChildren();
      setScreen('busy', 'Экран открыт в другой вкладке этого браузера.');
    };
  }

  // ---- ввод с клавиатуры ----
  function releaseCapture() {
    st.capturing = false;
    stage.classList.remove('is-capturing');
    if (controller) controller.blur();
    if (document.activeElement === kbd) kbd.blur();
    paintHint();
  }
  const startCapture = () => {
    if (st.control !== 'human') return;
    st.capturing = true;
    stage.classList.add('is-capturing');
    paintHint();
  };
  const stopCapture = () => {
    st.capturing = false;
    stage.classList.remove('is-capturing');
    paintHint();
  };
  host.addEventListener('focusin', startCapture);
  host.addEventListener('focusout', stopCapture);

  // Экранная клавиатура телефона: символы, Backspace и Enter уходят в RFB как нажатия клавиш. Скрытое поле только ловит ввод
  // и тут же стирается: текст нигде не копится.
  const sendKeys = (keys) => {
    if (st.control !== 'human' || st.screen !== 'live' || !controller) return;
    keys.forEach((key) => controller.sendKey(key));
  };
  const flushKeyboard = () => {
    const text = kbd.value;
    kbd.value = '';
    if (text) sendKeys(Array.from(text));
  };
  kbd.addEventListener('focus', startCapture);
  kbd.addEventListener('blur', stopCapture);
  kbd.addEventListener('keydown', (e) => {
    if (e.isComposing || (e.key !== 'Backspace' && e.key !== 'Enter')) return;
    e.preventDefault();
    sendKeys([e.key]);
  });
  // Клавиатуры Android шлют Backspace и Enter без имени клавиши: ловим их по типу ввода.
  kbd.addEventListener('beforeinput', (e) => {
    if (e.isComposing) return;
    if (e.inputType === 'deleteContentBackward') { e.preventDefault(); sendKeys(['Backspace']); }
    else if (e.inputType === 'insertLineBreak' || e.inputType === 'insertParagraph') { e.preventDefault(); sendKeys(['Enter']); }
  });
  kbd.addEventListener('input', (e) => { if (!e.isComposing) flushKeyboard(); });
  kbd.addEventListener('compositionend', flushKeyboard);
  function openKeyboard() {
    if (st.control !== 'human' || st.screen !== 'live') return;
    kbd.focus();
  }
  stage.addEventListener('keydown', (e) => {
    if (e.target !== stage) return;
    if ((e.key === 'Enter' || e.key === ' ') && st.control === 'human' && st.screen === 'live' && controller) {
      e.preventDefault();
      controller.focus();
    }
  });
  // Shift+Esc выходит из экрана к обычной навигации: Tab внутри экрана уходит в браузер бота. Слушатель на window в фазе
  // перехвата срабатывает раньше noVNC и не пускает эту клавишу в удалённый браузер.
  const onWindowKey = (e) => {
    if (e.key !== 'Escape') return;
    if (e.shiftKey && st.capturing) {
      e.preventDefault();
      e.stopPropagation();
      releaseCapture();
      stage.focus();
      announce('Ввод в браузер бота выключен, фокус на экране.');
    } else if (!e.shiftKey && st.fullscreen && !st.capturing) {
      toggleFullscreen(false);
    }
  };
  window.addEventListener('keydown', onWindowKey, true);

  // ---- масштаб и полный экран ----
  function setFit(fit) {
    st.fit = fit;
    const button = $('[data-br="scale"]', app);
    button.setAttribute('aria-pressed', String(fit));
    button.setAttribute('aria-label', fit ? 'Масштаб: по размеру окна' : 'Масштаб: 100%');
    button.innerHTML = fit ? ICONS.fit : ICONS.actual;
    host.classList.toggle('is-actual', !fit);
    if (controller) controller.setFit(fit);
  }
  function toggleFullscreen(on) {
    st.fullscreen = on;
    root.classList.toggle('is-fs', on);
    document.body.classList.toggle('br-fs-open', on);
    const button = $('[data-br="fullscreen"]', app);
    if (button) {
      button.setAttribute('aria-pressed', String(on));
      button.setAttribute('aria-label', on ? 'Выйти из полного экрана' : 'Во весь экран');
      button.innerHTML = on ? ICONS.shrink : ICONS.expand;
    }
    const wrap = el('br-stage-wrap');
    // Инструменты живут в полосе над экраном; во весь экран они переезжают на картинку (в режиме Fullscreen API видно только её).
    const tools = el('br-tools');
    const hadFocus = tools.contains(document.activeElement) ? document.activeElement : null;
    (on ? wrap : el('br-strip')).appendChild(tools);
    if (hadFocus) hadFocus.focus();
    paintActions();
    // Где нет Fullscreen API (iPhone), остаётся режим во всю страницу: экран закрывает всё поверх приложения.
    if (on && document.fullscreenEnabled && wrap && wrap.requestFullscreen) {
      fsViaApi = true;
      wrap.requestFullscreen().catch(() => { fsViaApi = false; });
    }
    if (!on && document.fullscreenElement && document.exitFullscreen) document.exitFullscreen().catch(() => { /* уже вышли */ });
  }
  const onFsChange = () => { if (!document.fullscreenElement && st.fullscreen && fsViaApi) { fsViaApi = false; toggleFullscreen(false); } };
  document.addEventListener('fullscreenchange', onFsChange);

  // ---- шаги, адрес, расход ----
  function paintAddress() {
    const a = el('br-addr');
    if (!a) return;
    a.textContent = st.address ? midTruncate(st.address, 44) : 'Адрес появится после первого перехода';
    a.title = st.address || '';
    a.classList.toggle('is-empty', !st.address);
  }
  function paintSpend() {
    const s = st.spend;
    const node = el('br-spend');
    if (!node) return;
    const elapsed = s.start && s.last ? Math.max(0, (new Date(s.last) - new Date(s.start)) / 1000) : 0;
    const seconds = Math.max(s.seconds, elapsed);
    node.textContent = s.tokens > 0 || seconds > 0 ? `Расход задачи: ${fmtTokens(s.tokens)} токенов · ${fmtClock(seconds)}` : 'Расход задачи: нет данных';
  }
  function paintEmpty() {
    document.querySelectorAll('[data-steps-empty]').forEach((n) => { n.hidden = st.steps.length > 0; });
  }
  function fillList(list) {
    const limit = Number(list.dataset.limit) || STEP_LIMIT;
    list.innerHTML = st.steps.slice(0, limit).map(stepHtml).join('');
  }
  function addStep(ev) {
    const model = stepModel(ev);
    st.steps.unshift(model);
    if (st.steps.length > STEP_LIMIT) st.steps.length = STEP_LIMIT;
    const payload = ev.payload || {};
    if (payload.action === 'navigate' && payload.url) { st.address = String(payload.url); paintAddress(); }
    return model;
  }
  function pushStepDom(model) {
    document.querySelectorAll('[data-steps-list]').forEach((list) => {
      const limit = Number(list.dataset.limit) || STEP_LIMIT;
      list.insertAdjacentHTML('afterbegin', stepHtml(model));
      while (list.children.length > limit) list.lastElementChild.remove();
    });
    paintEmpty();
  }
  function trackSpend(ev) {
    const s = st.spend;
    if (ev.kind === 'user_msg') { s.tokens = 0; s.seconds = 0; s.start = null; s.last = null; }
    if (ev.ts) { if (!s.start) s.start = ev.ts; s.last = ev.ts; }
    if (ev.kind === 'usage') { s.tokens += (ev.payload.tokens_in || 0) + (ev.payload.tokens_out || 0); s.seconds += ev.payload.seconds || 0; }
  }
  function onEvent(ev, { quiet = false } = {}) {
    if (!ev || ev.seq == null) return;
    const key = `${st.threadId}:${ev.seq}`;
    if (st.seen.has(key)) return;
    st.seen.add(key);
    trackSpend(ev);
    if (ev.kind === 'browser_step') {
      const model = addStep(ev);
      if (!quiet) pushStepDom(model);
    } else if (ev.kind === 'browser_control' && !quiet) {
      refresh();
    } else if ((ev.kind === 'approval_req' || ev.kind === 'approval_dec') && !quiet) {
      refreshApprovals();
    }
    if (!quiet) paintSpend();
  }

  // ---- подтверждения действий бота ----
  const isBrowserApproval = (a) => a.bot_id === botId && /browser/.test(a.tool || '');
  function dataText(args) {
    const skip = new Set(['url', 'target', 'host', 'reversible']);
    const parts = Object.entries(args || {}).filter(([k]) => !skip.has(k)).slice(0, 3)
      .map(([k, v]) => (SECRET_KEYS.test(k) ? `${k}: ••••••` : String(v)));
    return parts.join(', ') || '–';
  }
  function reversibleText(a) {
    const flag = a.args && a.args.reversible;
    if (flag === true) return 'Можно';
    if (flag === false || IRREVERSIBLE.includes(a.risk)) return 'Нельзя';
    return 'Не гарантировано';
  }
  function approvalHtml(a) {
    const dest = (a.args && (a.args.url || a.args.target || a.args.host)) || '';
    return `<section class="br-ask risk-card" role="region" aria-labelledby="br-ask-title" data-approval-id="${esc(a.id)}">
      <div class="risk-tag">${ICONS.alert}${esc(RISK_LABEL[a.risk] || 'ПОДТВЕРЖДЕНИЕ')}</div>
      <div class="t-callout br-ask-title" id="br-ask-title">${esc(a.title)}</div>
      <dl class="kv"><dt>Куда</dt><dd title="${esc(dest)}" data-i18n-skip>${esc(midTruncate(dest || '–', 40))}</dd><dt>Данные</dt><dd data-i18n-skip>${esc(dataText(a.args))}</dd><dt>Отменить</dt><dd>${esc(reversibleText(a))}</dd></dl>
      <p class="t-footnote br-ask-note">Действие не выполнено, страница не тронута, пока нет решения.</p>
      <div class="form-alert" data-ask-alert></div>
      <div class="btn-row btn-row-2">
        <button type="button" class="btn btn-secondary" data-br="reject" data-id="${esc(a.id)}">Отклонить</button>
        <button type="button" class="btn btn-attention" data-br="approve" data-id="${esc(a.id)}">Разрешить</button>
      </div>
    </section>`;
  }
  function paintApproval() {
    const box = el('br-approval');
    if (!box) return;
    const a = st.approvals[0];
    if (!a || (st.screen !== 'live' && st.screen !== 'connecting')) { box.hidden = true; box.innerHTML = ''; return; }
    if (box.firstElementChild && box.firstElementChild.dataset.approvalId === a.id) return;
    box.innerHTML = approvalHtml(a);
    box.hidden = false;
  }
  async function refreshApprovals() {
    if (disposed) return;
    try {
      const list = await api.listApprovals('pending');
      if (disposed) return;
      st.approvals = list.filter(isBrowserApproval);
      paintApproval();
    } catch { /* следующий опрос повторит */ }
  }
  async function decide(id, decision, button) {
    const box = $('[data-ask-alert]', app);
    const buttons = Array.from(button.closest('.btn-row').querySelectorAll('button'));
    buttons.forEach((b) => { b.disabled = true; });
    if (box) box.innerHTML = '';
    try {
      await api.decideApproval(id, decision);
      st.approvals = st.approvals.filter((a) => a.id !== id);
      const card = el('br-approval');
      card.hidden = true;
      card.innerHTML = '';
      flash(decision === 'approve' ? 'Действие разрешено' : 'Действие отклонено');
      paintApproval();
    } catch (err) {
      buttons.forEach((b) => { b.disabled = false; });
      if (err && err.status === 409) { await refreshApprovals(); flash('Решение уже не нужно', 'Подтверждение сброшено или решено в другом месте.'); return; }
      if (box) setAlert(box, 'Решение не отправлено', 'Сервер не ответил. Действие не выполнено, попробуйте ещё раз.');
    }
  }

  // ---- перехват и возврат ----
  function askTakeover() {
    flash('');
    dialog = openDialog({
      title: 'Перехватить управление?', subtitle: name,
      content: confirmBody({ text: 'Текущая задача бота будет остановлена. Браузер останется на этой странице, а подтверждения, которые ждали ответа, сбросятся.', confirmLabel: 'Перехватить', danger: false }),
      focus: 'first',
    });
    dialog.onClose = () => { dialog = null; };
    wireConfirm(dialog, {
      run: async () => {
        const button = $('[data-confirm]', dialog.el);
        const idle = button.innerHTML;
        setBusy(button, true, 'Перехватываю', idle);
        st.busy = true;
        try {
          const next = await api.browserTakeover(botId);
          st.returnLock = true; // кнопка «Вернуть боту» появится занятой
          applyServerState(next, { own: true });
          flash('');
        } catch (err) {
          st.returnLock = false;
          setBusy(button, false, '', idle);
          if (err && err.status === 409) {
            // Состояние на сервере уже другое: показываем его, а не своё предположение.
            st.busy = false;
            await refresh();
            flash('Состояние уже изменилось', 'Управление не передано: экран показывает актуальное состояние на сервере.');
            return;
          }
          throw err;
        } finally {
          st.busy = false;
        }
      },
      describeError: (err) => (err && err.status === 502 || err && err.status === 503
        ? { title: 'Управление не передано, бот продолжает работать', text: 'Сервер компьютеров не ответил. Попробуйте ещё раз.' }
        : { title: 'Нет связи с сервером', text: 'Управление не передано, бот продолжает работать.' }),
      // Фокус на экран, а не на кнопку: второй тап по «Перехватить» не должен тут же вернуть управление боту.
      done: () => {
        if (st.returnLock) {
          clearTimeout(returnLockTimer);
          returnLockTimer = setTimeout(() => { if (!disposed) unlockReturn(); }, RETURN_LOCK_MS);
        }
        if (st.screen === 'live' || st.screen === 'connecting' || st.screen === 'reconnecting') stage.focus();
        else { const b = $('[data-br="return"]', app); if (b) b.focus(); }
      },
    });
  }

  async function doReturn(button) {
    flash('');
    const idle = button.innerHTML;
    st.busy = true;
    setBusy(button, true, 'Возвращаю', idle);
    try {
      applyServerState(await api.browserReturn(botId), { own: true });
    } catch (err) {
      st.busy = false;
      if (err && err.status === 409) {
        await refresh();
        flash('Состояние уже изменилось', 'Экран показывает актуальное состояние на сервере.');
      } else {
        paintActions();
        flash('Управление не возвращено', 'Вы по-прежнему управляете браузером бота. Попробуйте ещё раз.', 'danger');
      }
      return;
    } finally {
      st.busy = false;
    }
    const next = $('[data-br="takeover"]', app);
    if (next) next.focus();
  }

  // ---- скрытый ввод пароля ----
  function openSecret() {
    flash('');
    const where = st.address ? hostOf(st.address) : name;
    const content = `<p class="t-body br-guarantee">${GUARANTEE}</p>
      <form id="sec-form" class="stack gap-3" novalidate autocomplete="off">
        <div class="form-field">
          <label for="sec-value">Пароль</label>
          <div class="input-row"><input id="sec-value" name="br-hidden-value" class="input" type="password" autocomplete="off" autocapitalize="none" autocorrect="off" spellcheck="false" maxlength="4096" aria-required="true" aria-describedby="sec-value-note"></div>
          <span id="sec-value-note" class="field-note" data-hint="" aria-live="polite"></span>
        </div>
        <label class="br-switch"><input type="checkbox" id="sec-save" role="switch"><span class="stack"><span class="t-body">Сохранить в хранилище секретов</span><span class="t-footnote">Значение хранится на сервере в зашифрованном виде.</span></span></label>
        <div class="form-field" id="sec-name-wrap" hidden>
          <label for="sec-name">Имя секрета</label>
          <div class="input-row"><input id="sec-name" name="br-secret-name" class="input mono" type="text" autocomplete="off" autocapitalize="none" autocorrect="off" spellcheck="false" maxlength="64" placeholder="site_password" aria-describedby="sec-name-note"></div>
          <span id="sec-name-note" class="field-note" data-hint="${NAME_HINT}">${NAME_HINT}</span>
        </div>
        <details class="br-details"><summary>Что это значит<span class="br-chev" aria-hidden="true">${ICONS.chevronDown}</span></summary>
          <ul class="br-details-list">
            <li>Защита от модели: она не получает экран и не действует, пока управляете вы, поэтому пароль не попадает в её контекст.</li>
            <li>Программа, запущенная в компьютере бота, может подсмотреть ввод. Если там работает чужой код, пароль для него не секрет.</li>
            <li>Для важных аккаунтов заведите отдельный пароль или включите вход с подтверждением.</li>
          </ul>
        </details>
        <div class="form-alert" id="sec-alert"></div>
        <div class="btn-row btn-row-2"><button type="button" class="btn btn-secondary" data-close>Отмена</button><button type="submit" class="btn btn-primary" id="sec-send">Ввести</button></div>
      </form>`;
    dialog = openDialog({ title: 'Пароль для сайта', subtitle: `${where} · вводите вы`, content, focus: 'first' });
    dialog.onClose = () => { dialog = null; const field = $('#sec-value'); if (field) field.value = ''; };
    const form = $('#sec-form', dialog.el);
    const input = $('#sec-value', dialog.el);
    const save = $('#sec-save', dialog.el);
    const nameWrap = $('#sec-name-wrap', dialog.el);
    const nameInput = $('#sec-name', dialog.el);
    const send = $('#sec-send', dialog.el);
    save.addEventListener('change', () => {
      nameWrap.hidden = !save.checked;
      if (save.checked) nameInput.focus();
    });
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      clearErrors(form);
      let value = input.value;
      if (!value) { setFieldError(input, 'Введите пароль.'); input.focus(); return; }
      const saveAs = save.checked ? nameInput.value.trim() : '';
      if (save.checked && !SECRET_NAME.test(saveAs)) { setFieldError(nameInput, 'Имя должно начинаться с буквы: латиница, цифры, _ и -, до 64 символов.'); nameInput.focus(); return; }
      const idle = send.innerHTML;
      form.querySelectorAll('input, button').forEach((c) => { c.disabled = true; });
      setBusy(send, true, 'Ввожу', idle);
      // Поле стирается сразу при отправке: значение живёт только в этой переменной до ответа сервера.
      input.value = '';
      try {
        await api.browserSecretInput(botId, value, saveAs || undefined);
      } catch (err) {
        value = '';
        form.querySelectorAll('input, button').forEach((c) => { c.disabled = false; });
        setBusy(send, false, '', idle);
        const msg = err && err.status === 409 && /human/.test(err.code || '') ? ['Управление уже у бота', 'Перехватите управление и повторите.']
          : err && err.status === 409 ? ['Экран не подключён', 'Дождитесь картинки экрана и повторите.']
            : err && err.status === 400 ? ['Пароль не введён', 'В пароле есть управляющие символы.']
              : ['Пароль не введён', 'Сервер не ответил. Введите пароль заново.'];
        setAlert($('#sec-alert', dialog.el), msg[0], msg[1]);
        return;
      }
      value = '';
      dialog.close();
      flash('Пароль введён в страницу', 'Значение нигде не сохранено на этом устройстве.');
      const next = $('[data-br="secret"]', app);
      if (next) next.focus();
    });
  }

  // ---- все шаги листом (телефон) ----
  function openAllSteps() {
    dialog = openDialog({
      title: 'Шаги', subtitle: `${name} · новые сверху`,
      content: `<p class="t-footnote" data-steps-empty${st.steps.length ? ' hidden' : ''}>${STEPS_EMPTY}</p><ol class="br-step-list" data-steps-list data-limit="${STEP_LIMIT}" aria-label="Шаги бота в браузере, новые сверху"></ol>`,
      focus: 'dialog',
    });
    dialog.onClose = () => { dialog = null; };
    fillList($('[data-steps-list]', dialog.el));
  }

  // ---- команды ----
  const onClick = (e) => {
    const target = e.target.closest('[data-br]');
    if (disposed || !target || !app.contains(target)) return;
    const act = target.getAttribute('data-br');
    if (act === 'takeover') { if (st.fullscreen) toggleFullscreen(false); askTakeover(); }
    else if (act === 'return') doReturn(target);
    else if (act === 'secret') openSecret();
    else if (act === 'keyboard') openKeyboard();
    else if (act === 'scale') setFit(!st.fit);
    else if (act === 'fullscreen') toggleFullscreen(!st.fullscreen);
    else if (act === 'all-steps') openAllSteps();
    else if (act === 'approve' || act === 'reject') decide(target.dataset.id, act, target);
    else if (act === 'reconnect') { st.busyRetries = 1; st.auto410 = 1; connect(); }
    else if (act === 'claim') claimHere();
    else if (act === 'recreate') recreateBotAction(target);
    else if (act === 'reload') location.reload();
  };
  app.addEventListener('click', onClick);

  // ---- запуск ----
  async function init() {
    if (!bot) { deniedText = null; setScreen('denied'); return; }
    if (!hasBrowser(bot)) { setScreen('none'); return; }
    setScreen('connecting');
    paintAddress();
    paintSpend();
    paintEmpty();
    const [state, thread] = await Promise.allSettled([api.getBrowser(botId), api.getThreadByBot(botId)]);
    if (disposed) return;
    if (state.status === 'rejected') {
      const err = state.reason;
      if (err && err.status === 401) return;
      if (err && (err.status === 403 || err.status === 404)) { deniedText = null; setScreen('denied'); return; }
      st.lostReason = 'network';
      setScreen('lost');
      return;
    }
    applyServerState(state.value);
    paintControl();
    st.threadId = thread.status === 'fulfilled' && thread.value ? thread.value.id : null;
    connect();
    refreshApprovals();
    if (st.threadId) {
      let events = [];
      try { events = await api.getEvents(st.threadId, 0); } catch { /* хвост дочитает поток */ }
      if (disposed) return;
      const sorted = (events || []).slice().sort((a, b) => a.seq - b.seq);
      for (const ev of sorted) onEvent(ev, { quiet: true });
      document.querySelectorAll('[data-steps-list]').forEach(fillList);
      paintEmpty();
      paintSpend();
      const since = sorted.reduce((m, ev) => Math.max(m, ev.seq || 0), 0);
      stopStream = openThreadStream(st.threadId, since, (ev) => onEvent(ev));
    }
    pollTimer = setInterval(() => { if (document.visibilityState === 'visible') refresh(); }, POLL_MS);
  }

  const onVisible = () => {
    if (document.visibilityState === 'visible' && st.screen === 'lost' && st.lostReason !== 'network' && !disposed) { st.busyRetries = 1; connect(); }
  };
  document.addEventListener('visibilitychange', onVisible);

  setCleanup(() => {
    disposed = true;
    connSeq += 1;
    clearInterval(pollTimer);
    clearTimeout(returnLockTimer);
    if (stopStream) stopStream();
    if (controller) { try { controller.close(); } catch { /* уже закрыт */ } }
    if (channel) channel.close();
    app.removeEventListener('click', onClick);
    window.removeEventListener('keydown', onWindowKey, true);
    document.removeEventListener('fullscreenchange', onFsChange);
    document.removeEventListener('visibilitychange', onVisible);
    document.body.classList.remove('br-fs-open');
    if (document.fullscreenElement && document.exitFullscreen) document.exitFullscreen().catch(() => { /* уже вышли */ });
    if (dialog) dialog.close();
  });

  init();
}

