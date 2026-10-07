// activity.js: экран «Активность» (docs/contracts.md, раздел 16): состояние ботов с паузой, «Пауза всех»,
// общая лента действий по всем ботам с группировкой по дням, фильтрами по боту и виду и подгрузкой по курсору.
// Ядро отдаёт у каждого события короткий код и параметры (title.code, title.params), строку собирает describe():
// так текст переводится каталогом i18n и ядро не зависит от языка интерфейса. Всё, что пришло с сервера и написано
// человеком или ботом (имена, заголовки подтверждений, цели шагов, адреса, тексты памяти), экранируется esc() и
// помечается data-i18n-skip.
import * as api from './api.js';
import { locale } from './i18n.js';
import { ICONS, esc, alertHtml } from './ui.js';
import { avatarHtml } from './avatars.js';
import { context, openDialog, confirmBody, wireConfirm, setAlert, setBusy, loadingHtml, stateHtml, retryButton } from './account.js';

const PAGE_SIZE = 30;
const REASON_MAX = 200;  // предел ядра для reason в /pause и /pause-all (docs/contracts.md, раздел 16)

const KINDS = [
  { value: 'turn', label: 'Задачи', icon: ICONS.play },
  { value: 'approval', label: 'Решения', icon: ICONS.approvals },
  { value: 'browser', label: 'Браузер', icon: ICONS.browser },
  { value: 'takeover', label: 'Передача управления', icon: ICONS.handoff },
  { value: 'schedule', label: 'Рутины', icon: ICONS.routines },
  { value: 'procedure', label: 'Процедуры', icon: ICONS.checklist },
  { value: 'memory', label: 'Память', icon: ICONS.memory },
  { value: 'group', label: 'Обсуждения', icon: ICONS.users },
  { value: 'pause', label: 'Паузы', icon: ICONS.stop },
];
const KIND_ICON = Object.fromEntries(KINDS.map((k) => [k.value, k.icon]));

// Подписи по коду события. Незнакомый код (ядро новее клиента) не ломает ленту: показывается как «Событие».
const TITLES = {
  turn_started: 'Задача начата',
  turn_done: 'Задача завершена',
  turn_error: 'Задача завершилась ошибкой',
  turn_stopped: 'Задача остановлена',
  compact_started: 'Сжатие контекста начато',
  compact_done: 'Контекст сжат',
  compact_failed: 'Сжатие контекста не удалось',
  approval_requested: 'Запрошено подтверждение',
  approval_approved: 'Подтверждение разрешено',
  approval_rejected: 'Подтверждение отклонено',
  checker_denied: 'Проверяющая модель отклонила действие',
  approval_expired: 'Срок подтверждения истёк',
  takeover_started: 'Вы перехватили браузер',
  takeover_returned: 'Вы вернули управление боту',
  takeover_bot: 'Бот снова управляет браузером',
  takeover_changed: 'Управление браузером изменилось',
  schedule_run: 'Запуск по расписанию',
  hook_run: 'Запуск по событию (webhook)',
  schedule_resumed: 'Расписание возобновлено',
  wakeup_scheduled: 'Бот запланировал пробуждение',
  wakeup_fired: 'Бот проснулся по своему запросу',
  delegation_sent: 'Бот передал задачу другому боту',
  delegation_done: 'Поручение выполнено',
  procedure_started: 'Запущена процедура',
  procedure_finished: 'Процедура завершена',
  memory_proposed: 'Бот предложил запомнить',
  bot_paused: 'Бот поставлен на паузу',
  bot_resumed: 'Бот возобновил работу',
  group_started: 'Началось обсуждение ботов',
  group_finished: 'Обсуждение ботов завершено',
};
const SKIPPED = {
  executor_unavailable: 'Запуск пропущен: компьютер бота недоступен',
  bot_paused: 'Запуск пропущен: бот на паузе',
  provider_unavailable: 'Запуск пропущен: модель бота недоступна',
  check_failed: 'Запуск пропущен: проверка исполнителя не удалась',
};
const WAKEUP_SKIPPED = {
  executor_unavailable: 'Пробуждение пропущено: компьютер бота недоступен',
  bot_paused: 'Пробуждение пропущено: бот на паузе',
  provider_unavailable: 'Пробуждение пропущено: модель бота недоступна',
  check_failed: 'Пробуждение пропущено: проверка исполнителя не удалась',
};
const DELEGATION_END = {
  done: 'Поручение выполнено',
  error: 'Поручение завершилось ошибкой',
  stopped: 'Поручение остановлено',
};
const BROWSER_ACTIONS = {
  navigate: 'Открыта страница',
  click: 'Нажатие на странице',
  fill: 'Заполнено поле',
  snapshot: 'Снимок страницы',
  screenshot: 'Скриншот страницы',
};
const PROCEDURE_END = {
  done: 'Процедура завершена',
  failed: 'Процедура завершилась ошибкой',
  stopped: 'Процедура остановлена',
};
const RISK_LABEL = { pay: 'ОПЛАТА', send: 'ОТПРАВКА', delete: 'УДАЛЕНИЕ', login: 'ВХОД', push: 'ПУБЛИКАЦИЯ', exec: 'КОМАНДА', other: 'ДЕЙСТВИЕ' };
const RISK_BADGE = { pay: 'badge-danger', delete: 'badge-danger', login: 'badge-danger', send: 'badge-attention', push: 'badge-attention', exec: 'badge-attention' };

export function describe(item) {
  const code = item && item.title && item.title.code;
  const params = (item && item.title && item.title.params) || {};
  if (code === 'schedule_skipped') return SKIPPED[params.reason] || 'Запуск пропущен: исполнитель недоступен';
  if (code === 'wakeup_skipped') return WAKEUP_SKIPPED[params.reason] || 'Пробуждение пропущено: исполнитель недоступен';
  if (code === 'delegation_done') return DELEGATION_END[params.outcome] || TITLES.delegation_done;
  if (code === 'browser_step') return BROWSER_ACTIONS[params.action] || 'Действие в браузере';
  if (code === 'procedure_finished') return PROCEDURE_END[item.status] || TITLES.procedure_finished;
  return TITLES[code] || 'Событие';
}

// Вторая строка: то, что написал человек или бот, и счётчики. Возвращает [{ text, user }]: user помечается data-i18n-skip.
export function details(item) {
  const params = (item.title && item.title.params) || {};
  const out = [];
  switch (item.title && item.title.code) {
    case 'browser_step':
      if (params.target && params.target !== '[redacted]') out.push({ text: params.target, user: true });
      if (params.url) out.push({ text: params.url, user: true });
      if (item.status === 'error') out.push({ text: 'Шаг не удался', user: false });
      break;
    case 'schedule_run':
    case 'hook_run':
      if (params.name) out.push({ text: params.name, user: true });
      break;
    case 'schedule_skipped':
    case 'schedule_resumed':
      if (params.name) out.push({ text: params.name, user: true });
      if (params.paused) out.push({ text: 'Расписание на паузе, возобновится автоматически', user: false });
      break;
    case 'group_started':
    case 'group_finished':
      if (params.title) out.push({ text: params.title, user: true });
      break;
    case 'procedure_started':
    case 'procedure_finished':
      if (params.name) out.push({ text: params.name, user: true });
      break;
    case 'delegation_sent':
    case 'delegation_done':
      if (params.from_bot && params.to_bot) out.push({ text: `${params.from_bot} → ${params.to_bot}`, user: true });
      break;
    case 'bot_paused':
      if (params.reason) out.push({ text: params.reason, user: true });
      break;
    default:
      break;
  }
  if (item.detail) out.push({ text: item.detail, user: true });
  return out;
}

// Куда ведёт строка: тред, запуск процедуры или рутина; без цели строка остаётся простым текстом.
export function targetHref(item) {
  const params = (item.title && item.title.params) || {};
  if (item.kind === 'procedure' && params.run_id) return `#/procedure-runs/${encodeURIComponent(params.run_id)}`;
  if (item.thread_id && (item.kind === 'group' || /^group_/.test((item.title && item.title.code) || ''))) return `#/groups/${encodeURIComponent(item.thread_id)}`;
  if (item.thread_id) return `#/threads/${encodeURIComponent(item.thread_id)}`;
  if (item.kind === 'schedule' && params.schedule_id) return `#/routines/${encodeURIComponent(params.schedule_id)}`;
  return '';
}

const SKIP_NOTE = {
  executor_unavailable: (n) => `Пропущено ${n} раз: компьютер бота недоступен`,
  bot_paused: (n) => `Пропущено ${n} раз: бот на паузе`,
  provider_unavailable: (n) => `Пропущено ${n} раз: модель бота недоступна`,
};
// Метки расписания на экране рутин: сколько запусков подряд пропущено и почему; после 5 пропусков расписание
// проверяется реже и возобновится само, когда исполнитель вернётся.
export function skipNoteHtml(schedule) {
  const count = Number(schedule && schedule.skipped_count) || 0;
  if (!count) return '';
  const note = (SKIP_NOTE[schedule.last_skip_reason] || ((n) => `Пропущено ${n} раз: исполнитель недоступен`))(count);
  return `<span class="row-meta act-skip" data-skip-note>${esc(note)}</span>`
    + (schedule.paused_by_unavailable ? '<span class="row-meta act-skip" data-skip-auto>Возобновится автоматически</span>' : '');
}

// ---- время и группировка по дням ----
const dayKey = (date) => `${date.getFullYear()}-${date.getMonth()}-${date.getDate()}`;
function dayLabel(date, today = new Date()) {
  if (dayKey(date) === dayKey(today)) return 'Сегодня';
  const yesterday = new Date(today);
  yesterday.setDate(today.getDate() - 1);
  if (dayKey(date) === dayKey(yesterday)) return 'Вчера';
  const opts = { day: 'numeric', month: 'long' };
  if (date.getFullYear() !== today.getFullYear()) opts.year = 'numeric';
  return date.toLocaleDateString(locale(), opts);
}
const timeLabel = (date) => date.toLocaleTimeString(locale(), { hour: '2-digit', minute: '2-digit' });

export function groupByDay(items, today = new Date()) {
  const groups = [];
  for (const item of items) {
    const date = new Date(item.at);
    if (Number.isNaN(date.getTime())) continue;
    const key = dayKey(date);
    let group = groups[groups.length - 1];
    if (!group || group.key !== key) {
      group = { key, label: dayLabel(date, today), items: [] };
      groups.push(group);
    }
    group.items.push(item);
  }
  return groups;
}

// ---- состояние бота ----
const RUNNING = ['running', 'waiting', 'waiting_approval', 'waiting_mac'];
function botLine(bot) {
  if (bot.paused) return { text: 'На паузе', kind: 'attention' };
  if (RUNNING.includes(bot.status)) return { text: 'Идёт задача', kind: 'success' };
  if (bot.status === 'starting') return { text: 'Запускается', kind: 'attention' };
  if (bot.status === 'error_starting') return { text: 'Компьютер не запустился', kind: 'danger' };
  return { text: 'Готов к задачам', kind: 'neutral' };
}

function pausedSince(bot) {
  if (!bot.paused_at) return '';
  const date = new Date(bot.paused_at);
  return Number.isNaN(date.getTime()) ? '' : `${dayLabel(date)}, ${timeLabel(date)}`;
}

function failText(err) {
  return err && err.status ? 'Сервер ответил ошибкой. Повторите через минуту.' : 'Проверьте сеть или VPN и повторите.';
}

export async function viewActivity() {
  const c = context();
  const app = c.app;
  const state = {
    bots: [], botsError: false, botFilter: '', kinds: new Set(), items: [], next: null,
    feed: 'loading', feedSeq: 0, moreBusy: false, notices: [], exportBusy: false,
  };

  await c.frame({
    title: 'Активность',
    subtitle: 'Что делали боты',
    backHref: '#/settings',
    activeNav: 'activity',
    body: `<div class="page-narrow stack gap-4" id="act">
      <section class="stack gap-2" aria-labelledby="act-bots-h">
        <div class="act-head"><h2 class="section-label" id="act-bots-h">Состояние ботов</h2><div class="act-head-actions" id="act-bulk"></div></div>
        <div id="act-notice" class="stack gap-2" role="status"></div>
        <div id="act-bots">${loadingHtml('Загружаем ботов…')}</div>
      </section>
      <section class="stack gap-3" aria-labelledby="act-feed-h">
        <div class="act-head">
          <h2 class="section-label" id="act-feed-h">Журнал действий</h2>
          <div class="act-head-actions">
            <label class="sr-only" for="act-export-days">Период CSV</label>
            <select id="act-export-days" class="input">
              <option value="7" selected>7 дней</option>
              <option value="30">30 дней</option>
              <option value="90">90 дней</option>
            </select>
            <button type="button" class="btn btn-secondary" data-act="export-csv">${ICONS.download}Скачать CSV</button>
          </div>
        </div>
        <div id="act-filters" class="stack gap-2"></div>
        <div id="act-feed" tabindex="-1">${loadingHtml('Загружаем журнал…')}</div>
        <div id="act-more"></div>
      </section>
    </div>`,
  });
  const root = app.querySelector('#act');
  if (!root) return;
  const $ = (selector) => root.querySelector(selector);
  const botName = (id) => (state.bots.find((b) => b.id === id) || {}).name || id || '';
  const botById = (id) => state.bots.find((b) => b.id === id);

  // ---------- панель ботов ----------
  function paintBulk() {
    const any = state.bots.length > 0;
    const hasActive = state.bots.some((b) => !b.paused);
    const hasPaused = state.bots.some((b) => b.paused);
    $('#act-bulk').innerHTML = any
      ? `${hasActive ? '<button type="button" class="btn btn-secondary" data-act="pause-all">Пауза всех</button>' : ''}${hasPaused ? '<button type="button" class="btn btn-secondary" data-act="resume-all">Возобновить всех</button>' : ''}`
      : '';
  }

  function paintBots() {
    paintBulk();
    const box = $('#act-bots');
    if (state.botsError) {
      box.innerHTML = alertHtml('Не удалось загрузить ботов', failText(), 'danger') + `<div class="state-actions">${retryButton.replace('data-act="retry"', 'data-act="retry-bots"')}</div>`;
      return;
    }
    if (!state.bots.length) {
      box.innerHTML = stateHtml({ iconHtml: ICONS.bots, title: 'Ботов пока нет', text: 'Когда появится первый бот, его состояние и пауза будут здесь.', actions: '<a class="btn btn-secondary" href="#/bots/new">Новый бот</a>' });
      return;
    }
    box.innerHTML = `<ul class="settings-group list-plain act-bots">${state.bots.map((bot) => {
      const line = botLine(bot);
      const since = bot.paused ? pausedSince(bot) : '';
      return `<li class="act-bot" data-bot="${esc(bot.id)}">
        ${avatarHtml(bot.avatar || 'robot', bot.provider, 36)}
        <span class="row-body">
          <span class="row-title wrap" data-i18n-skip>${esc(bot.name)}</span>
          <span class="row-sub wrap"><span class="status-dot ${line.kind}" aria-hidden="true"></span> <span>${esc(line.text)}</span>${since ? ` <span data-i18n-skip>· ${esc(since)}</span>` : ''}</span>
          ${bot.paused && bot.paused_reason ? `<span class="row-sub wrap" data-i18n-skip>${esc(bot.paused_reason)}</span>` : ''}
        </span>
        <button type="button" role="switch" class="act-switch" aria-checked="${bot.paused ? 'true' : 'false'}" aria-label="Пауза бота ${esc(bot.name)}" data-act="toggle-pause" data-bot="${esc(bot.id)}"><span class="act-switch-label">Пауза</span><span class="act-switch-track" aria-hidden="true"><span class="act-switch-thumb"></span></span></button>
      </li>`;
    }).join('')}</ul>`;
  }

  function paintNotices() {
    $('#act-notice').innerHTML = state.notices.map((n) => `<div class="banner banner-info" data-notice-turn="${esc(n.turn)}">
      <span class="banner-icon">${ICONS.alert}</span>
      <span class="banner-text"><span class="banner-title">Бот на паузе, но текущая задача продолжает работать</span><span class="banner-sub" data-i18n-skip>${esc(botName(n.bot))}</span><span class="banner-sub">Пауза идущую задачу не останавливает.</span></span>
      <button type="button" class="btn btn-secondary" data-act="stop-turn" data-turn="${esc(n.turn)}" data-bot="${esc(n.bot)}">Остановить текущую задачу</button>
    </div>`).join('');
  }

  function applyPauseView(view) {
    const bot = botById(view.bot_id);
    if (bot) {
      bot.paused = view.paused;
      bot.paused_at = view.paused_at;
      bot.paused_reason = view.paused_reason;
    }
    state.notices = state.notices.filter((n) => n.bot !== view.bot_id);
    if (view.paused && view.running_turn) state.notices.push({ bot: view.bot_id, turn: view.running_turn });
  }

  async function loadBots() {
    state.botsError = false;
    try {
      state.bots = await api.listBots();
    } catch {
      state.botsError = true;
    }
    if (!root.isConnected) return;
    paintBots();
    paintFilters();
  }

  async function togglePause(button) {
    const id = button.getAttribute('data-bot');
    const bot = botById(id);
    if (!bot) return;
    button.disabled = true;
    try {
      applyPauseView(bot.paused ? await api.resumeBot(id) : await api.pauseBot(id));
    } catch (err) {
      button.disabled = false;
      $('#act-notice').insertAdjacentHTML('afterbegin', alertHtml('Не удалось изменить паузу', failText(err), 'danger'));
      return;
    }
    paintBots();
    paintNotices();
    root.querySelector(`[data-act="toggle-pause"][data-bot="${CSS.escape(id)}"]`)?.focus();
    loadFeed();
  }

  async function stopTurn(button) {
    const turn = button.getAttribute('data-turn');
    button.disabled = true;
    try {
      await api.stopTurn(turn);
    } catch (err) {
      button.disabled = false;
      $('#act-notice').insertAdjacentHTML('afterbegin', alertHtml('Не удалось остановить задачу', failText(err), 'danger'));
      return;
    }
    state.notices = state.notices.filter((n) => n.turn !== turn);
    paintNotices();
    loadBots();
    loadFeed();
  }

  function pauseAllDialog(trigger) {
    const busy = state.bots.filter((b) => !b.paused && RUNNING.includes(b.status));
    const running = busy.length
      ? `<p class="t-footnote" id="act-busy-hint">Сейчас идёт задача, пауза её не остановит. Остановить можно отдельно.</p><p class="t-footnote" data-i18n-skip>${esc(busy.map((b) => b.name).join(', '))}</p>`
      : '<p class="t-footnote" id="act-busy-hint">Идущие задачи пауза не останавливает: их можно остановить отдельно.</p>';
    const dlg = openDialog({
      title: 'Поставить всех ботов на паузу?',
      content: confirmBody({
        text: 'Боты перестанут брать новые задачи, расписания и webhook будут пропускаться. Сообщения в тредах дождутся возобновления.',
        confirmLabel: 'Пауза всех',
        danger: false,
      }),
    });
    dlg.body.querySelector('.confirm-text').insertAdjacentHTML('afterend', `${running}<div class="form-field">
        <label for="act-reason">Причина (необязательно)</label>
        <div class="input-row"><input id="act-reason" class="input" type="text" maxlength="${REASON_MAX}" autocomplete="off" aria-describedby="act-reason-note"></div>
        <span id="act-reason-note" class="field-note">Видна в журнале и у каждого бота на паузе. До 200 символов.</span>
      </div>`);
    dlg.body.querySelector('[data-close]').focus();  // поле вставлено выше кнопок, фокус по-прежнему на «Отмена», а не на действии
    wireConfirm(dlg, {
      run: async () => {
        const reason = dlg.body.querySelector('#act-reason').value.trim().slice(0, REASON_MAX);
        const result = await api.pauseAll(reason || undefined);
        state.notices = [];
        for (const view of result.bots || []) applyPauseView(view);
      },
      describeError: (err) => ({ title: 'Не удалось поставить на паузу', text: failText(err) }),
      done: () => { paintBots(); paintNotices(); (trigger && trigger.isConnected ? trigger : $('[data-act="resume-all"]'))?.focus?.(); loadFeed(); },
    });
  }

  async function resumeAll(button) {
    button.disabled = true;
    try {
      const result = await api.resumeAll();
      state.notices = [];
      for (const view of result.bots || []) applyPauseView(view);
    } catch (err) {
      button.disabled = false;
      $('#act-notice').insertAdjacentHTML('afterbegin', alertHtml('Не удалось возобновить', failText(err), 'danger'));
      return;
    }
    paintBots();
    paintNotices();
    loadFeed();
  }

  // ---------- фильтры ----------
  const isPressed = (group, value) => {
    if (group === 'bot') return state.botFilter === value;
    return value ? state.kinds.has(value) : state.kinds.size === 0;
  };

  function chip(group, value, label, { skip = false } = {}) {
    return `<button type="button" class="act-chip" data-chip="${group}" data-value="${esc(value)}" aria-pressed="${isPressed(group, value)}"${skip ? ' data-i18n-skip' : ''}>${esc(label)}</button>`;
  }

  // Панель фильтров собирается заново только когда пришёл список ботов; фокус остаётся на том же чипе.
  function paintFilters() {
    const box = $('#act-filters');
    const active = box.contains(document.activeElement) ? document.activeElement.closest('[data-chip]') : null;
    const keep = active && [active.getAttribute('data-chip'), active.getAttribute('data-value')];
    const bots = state.bots.map((b) => chip('bot', b.id, b.name, { skip: true })).join('');
    const kinds = KINDS.map((k) => chip('kind', k.value, k.label)).join('');
    box.innerHTML = `<div class="act-chips" role="group" aria-label="Фильтр по боту">${chip('bot', '', 'Все боты')}${bots}</div>
      <div class="act-chips" role="group" aria-label="Фильтр по виду">${chip('kind', '', 'Все виды')}${kinds}</div>`;
    if (keep) box.querySelector(`[data-chip="${keep[0]}"][data-value="${CSS.escape(keep[1])}"]`)?.focus();
  }

  async function exportCsv(button) {
    if (state.exportBusy) return;
    state.exportBusy = true;
    const days = Number($('#act-export-days').value) || 7;
    setBusy(button, true, 'Скачиваю', 'Скачать CSV');
    try {
      const csv = await api.exportActivityCsv(days);
      const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' });
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = `activity-${days}-days.csv`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (err) {
      setAlert($('#act-more'), 'Не удалось скачать CSV', failText(err));
    } finally {
      state.exportBusy = false;
      setBusy(button, false, '', 'Скачать CSV');
    }
  }

  // Выбор фильтра меняет только aria-pressed у существующих кнопок: узлы не пересоздаются, фокус остаётся на нажатом чипе.
  function syncChips() {
    root.querySelectorAll('[data-chip]').forEach((button) => {
      button.setAttribute('aria-pressed', String(isPressed(button.getAttribute('data-chip'), button.getAttribute('data-value'))));
    });
  }

  function pressChip(button) {
    const group = button.getAttribute('data-chip');
    const value = button.getAttribute('data-value');
    if (group === 'bot') state.botFilter = value;
    else if (!value) state.kinds.clear();
    else if (state.kinds.has(value)) state.kinds.delete(value);
    else state.kinds.add(value);
    syncChips();
    loadFeed();
  }

  // ---------- лента ----------
  function rowHtml(item, index) {
    const href = targetHref(item);
    const name = item.bot_id ? botName(item.bot_id) : '';
    const date = new Date(item.at);
    const lines = details(item).map((d) => `<span class="row-sub wrap"${d.user ? ' data-i18n-skip' : ''}>${esc(d.text)}</span>`).join('');
    const risk = item.risk ? `<span class="badge ${RISK_BADGE[item.risk] || 'badge-sunken'}">${esc(RISK_LABEL[item.risk] || RISK_LABEL.other)}</span>` : '';
    const inner = `<span class="row-icon" aria-hidden="true">${KIND_ICON[item.kind] || ICONS.activity}</span>
      <span class="row-body">
        <span class="row-title wrap"><span>${esc(describe(item))}</span>${risk ? ` ${risk}` : ''}</span>
        ${lines}
        <span class="row-meta">${name ? `<span data-i18n-skip>${esc(name)}</span> · ` : ''}<time datetime="${esc(item.at)}">${esc(Number.isNaN(date.getTime()) ? '' : timeLabel(date))}</time></span>
      </span>
      ${href ? `<span class="row-chevron" aria-hidden="true">${ICONS.chevronRight}</span>` : ''}`;
    return `<li>${href
      ? `<a class="list-row act-row" href="${href}" data-idx="${index}" data-item="${esc(item.id)}" data-kind="${esc(item.kind)}">${inner}</a>`
      : `<div class="list-row act-row" data-idx="${index}" data-item="${esc(item.id)}" data-kind="${esc(item.kind)}">${inner}</div>`}</li>`;
  }

  function paintFeed() {
    const box = $('#act-feed');
    const more = $('#act-more');
    more.innerHTML = '';
    if (state.feed === 'loading') { box.innerHTML = loadingHtml('Загружаем журнал…'); return; }
    if (state.feed === 'error') {
      box.innerHTML = stateHtml({ iconHtml: ICONS.alert, title: 'Не удалось загрузить журнал', text: 'Проверьте сеть или VPN и повторите.', actions: retryButton.replace('data-act="retry"', 'data-act="retry-feed"'), kind: 'state-error' });
      return;
    }
    if (!state.items.length) {
      const filtered = state.botFilter || state.kinds.size;
      box.innerHTML = stateHtml({
        iconHtml: ICONS.activity,
        title: filtered ? 'Ничего не найдено' : 'Пока нет событий',
        text: filtered ? 'По выбранным фильтрам событий нет.' : 'Здесь появятся запуски задач, подтверждения, действия в браузере и паузы.',
        actions: filtered ? '<button type="button" class="btn btn-secondary" data-act="reset-filters">Сбросить фильтры</button>' : '',
      });
      return;
    }
    let index = 0;
    box.innerHTML = groupByDay(state.items).map((group) => `<section class="act-day" aria-label="${esc(group.label)}">
      <h3 class="act-day-label">${esc(group.label)}</h3>
      <ul class="list-plain act-list">${group.items.map((item) => rowHtml(item, index++)).join('')}</ul>
    </section>`).join('');
    paintMore();
  }

  function paintMore() {
    $('#act-more').innerHTML = state.next
      ? `<div class="form-alert" id="act-more-alert"></div><button type="button" class="btn btn-secondary btn-block" data-act="more">Показать ещё</button>`
      : '';
  }

  function query(before) {
    return { botId: state.botFilter || undefined, kinds: [...state.kinds], before, limit: PAGE_SIZE };
  }

  async function loadFeed() {
    const seq = ++state.feedSeq;
    state.feed = 'loading';
    state.items = [];
    state.next = null;
    paintFeed();
    try {
      const page = await api.listActivity(query());
      if (seq !== state.feedSeq || !root.isConnected) return;
      state.items = page.items || [];
      state.next = page.next || null;
      state.feed = 'ready';
    } catch {
      if (seq !== state.feedSeq || !root.isConnected) return;
      state.feed = 'error';
    }
    paintFeed();
  }

  async function loadMore(button) {
    if (state.moreBusy || !state.next) return;
    state.moreBusy = true;
    const seq = state.feedSeq;
    const alertBox = $('#act-more-alert');
    if (alertBox) alertBox.innerHTML = '';
    setBusy(button, true, 'Загружаю', 'Показать ещё');
    try {
      const page = await api.listActivity(query(state.next));
      if (seq !== state.feedSeq || !root.isConnected) return;
      const known = new Set(state.items.map((i) => i.id));
      const firstNew = state.items.length;
      state.items = state.items.concat((page.items || []).filter((i) => !known.has(i.id)));
      state.next = page.next || null;
      paintFeed();
      (root.querySelector(`[data-idx="${firstNew}"]`) || $('#act-feed'))?.focus();
    } catch (err) {
      if (alertBox && alertBox.isConnected) setAlert(alertBox, 'Не удалось загрузить ещё', failText(err));
      setBusy(button, false, '', 'Показать ещё');
    } finally {
      state.moreBusy = false;
    }
  }

  root.addEventListener('click', (event) => {
    const chipButton = event.target.closest('[data-chip]');
    if (chipButton) { pressChip(chipButton); return; }
    const act = event.target.closest('[data-act]');
    if (!act) return;
    const action = act.getAttribute('data-act');
    if (action === 'toggle-pause') togglePause(act);
    else if (action === 'pause-all') pauseAllDialog(act);
    else if (action === 'resume-all') resumeAll(act);
    else if (action === 'stop-turn') stopTurn(act);
    else if (action === 'more') loadMore(act);
    else if (action === 'retry-feed') loadFeed();
    else if (action === 'retry-bots') { $('#act-bots').innerHTML = loadingHtml('Загружаем ботов…'); loadBots(); }
    else if (action === 'reset-filters') { state.botFilter = ''; state.kinds.clear(); syncChips(); loadFeed(); }
    else if (action === 'export-csv') exportCsv(act);
  });

  // Каркас и состояния загрузки уже на экране: данные догружаются в фоне. Если дождаться их здесь, render() держит экран
  // inert до конца запроса, и клавиатура с кликами не работают первые полсекунды.
  paintFilters();
  loadBots();
  loadFeed();
}
