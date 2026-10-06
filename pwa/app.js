// app.js: hash-роутер и экраны botstead PWA (без сборки).
import * as api from './api.js';
import { avatarHtml, AVATAR_KINDS, avatarLabel, randomAvatar } from './avatars.js';
import { openThreadStream } from './ws.js';
import { ICONS, icon, esc, backHeader, alertHtml, modelTitle, fmtDateTime } from './ui.js';
import { createAccount } from './account.js';
import { viewProviders, loadModelOptions, renderModelPicker, botModelCardHtml, mountBotModelCard, botStateBanner, botStartHint, recreateBotAction, confirmDeleteBot } from './providers.js';
import { botNoModel, runnerKind, usableModels, providerStatus } from './registry.js';
import { viewProviderRequests } from './provider-requests.js';
import { hasBrowser, browserBodyHtml, browserAsideHtml, mountBrowser, controlTitle } from './browser.js';
import { viewProcedureRoute, openSaveProcedureDialog } from './procedures.js';
import { viewMemory } from './memory.js';
import { viewActivity, skipNoteHtml } from './activity.js';
import { getLang, loadLang, locale } from './i18n.js';
import { startTranslator } from './i18n-dom.js';

const app = document.getElementById('app');

const PROVIDER_LABEL = { claude: 'Claude', codex: 'Codex', gemini: 'Gemini' };
const RISK_LABEL = { pay: 'ОПЛАТА', send: 'ОТПРАВКА', delete: 'УДАЛЕНИЕ', login: 'ВХОД', push: 'ПУБЛИКАЦИЯ', exec: 'КОМАНДА', other: 'ДЕЙСТВИЕ' };
// Человекочитаемое расписание для простых cron (минута час * * дни-недели);
// сложные выражения показываем как есть, cron мелким текстом рядом всё равно виден.
function humanizeCron(cron) {
  const parts = (cron || '').trim().split(/\s+/);
  if (parts.length !== 5) return cron || '';
  const [min, hour, dom, mon, dow] = parts;
  if (dom !== '*' || mon !== '*') return cron;
  const hh = /^\d{1,2}$/.test(hour) ? hour.padStart(2, '0') : null;
  const mm = /^\d{1,2}$/.test(min) ? min.padStart(2, '0') : null;
  const time = hh && mm ? `${hh}:${mm}` : null;
  const day = dow === '1-5' ? 'по будням' : (dow === '0,6' || dow === '6,0') ? 'по выходным' : 'каждый день';
  return time ? `${day} в ${time}` : day;
}

function fmtBytes(n) {
  if (n >= 1e6) return `${(n / 1e6).toFixed(1).replace('.', ',')} МБ`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(0)} КБ`;
  return `${n} Б`;
}
function fmtTokens(n) {
  if (n >= 1000) return `${Math.round(n / 1000)}k токенов`;
  return `${n} токенов`;
}
function badgeClass(provider) {
  return { claude: 'badge-claude', codex: 'badge-codex', gemini: 'badge-gemini' }[provider] || 'badge-sunken';
}
function statusDotClass(kind) {
  return { success: 'success', neutral: 'neutral', attention: 'attention', danger: 'danger' }[kind] || 'neutral';
}
function timeAgo(iso) {
  const sec = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
  if (sec < 60) return 'меньше минуты назад';
  const min = Math.round(sec / 60);
  if (min < 60) return `${min} мин назад`;
  const hr = Math.round(min / 60);
  if (hr < 24) return `${hr} ч назад`;
  return `${Math.round(hr / 24)} дн назад`;
}
// Реальный Mac-агент пока не шлёт net/latency_ms (docs/contracts.md), только
// host иногда — раньше тут была "Полный контроль ·  · – мс" из пустых полей.
function macStatusLine(mac) {
  if (mac.state === 'online') return `Полный контроль · ${esc(mac.info?.host || 'Mac')}`;
  if (mac.last_seen) return `Не в сети · последний контакт ${timeAgo(mac.last_seen)}`;
  return 'Ещё не подключался';
}

// Реальное /api/bots отдаёт только колонки из docs/contracts.md (status, executor, role...):
// status_label/status_kind/location/summary — только у мок-данных. Достраиваем их тут,
// чтобы карточки ботов не оставались пустыми на настоящем ядре.
const STATUS_LABEL = { starting: 'Запускается', idle: 'Готово', running: 'Работает', waiting: 'Ждёт решения', waiting_approval: 'Ждёт решения', waiting_mac: 'Ждёт Mac', stopped: 'Остановлен', error: 'Стоп' };
const STATUS_KIND = { starting: 'attention', idle: 'success', running: 'success', waiting: 'attention', waiting_approval: 'attention', waiting_mac: 'attention', stopped: 'neutral', error: 'danger' };
// Провайдеры из реестра по id: источник названия провайдера и его ошибки (ключ отклонён, адрес не отвечает) для ботов.
let providerById = new Map();
const HEALTHY_BOT_STATUS = ['idle', 'running', 'waiting', 'waiting_approval', 'waiting_mac'];

// Строка «провайдер · модель» шапки треда: одна на телефон и Mac, название провайдера из реестра, не имя раннера.
function botModelLine(bot) {
  const provider = bot.provider_name || PROVIDER_LABEL[bot.provider] || '';
  return [provider, modelTitle(bot.model)].filter(Boolean).join(' · ');
}

// Названия инструментов бота (mcp__bothub__mac_find_files) в основном тексте не показываем.
const TOOL_LABEL = { shell: 'Команды на компьютере бота', mac_find_files: 'Поиск файлов на Mac', mac_read_file: 'Чтение файлов на Mac', mac_delegate: 'Поручения Mac' };
function toolLabel(tool) {
  const name = String(tool || '').replace(/^mcp__.+?__/, '');
  return TOOL_LABEL[name] || name.replace(/_/g, ' ');
}
// Статусы реестра (no_model, error_starting) и флаг need_restart заданы ядром (docs/contracts.md §10–12):
// «нет модели» считается, пока у бота пусты provider_id или model_id, ядро само статус при привязке не снимает.
function botView(b) {
  const noModel = botNoModel(b);
  const startFailed = b.status === 'error_starting';
  const provider = providerById.get(b.provider_id);
  // Проблема провайдера (ключ отклонён, адрес не отвечает, нужен вход, ждёт администратора, не проверен): точка статуса не зелёная, пока модель бота недоступна.
  const providerError = !noModel && !startFailed && provider && ['error', 'pending_admin', 'unchecked'].includes(provider.status) && (!b.status || HEALTHY_BOT_STATUS.includes(b.status))
    ? providerStatus(provider) : null;
  return {
    ...b,
    noModel,
    provider_name: provider ? provider.name : '',
    location: b.location ?? (b.executor === 'mac' ? 'Mac' : 'Сервер'),
    status_label: noModel ? 'Нет модели' : startFailed ? 'Компьютер не запустился' : providerError ? `Провайдер: ${providerError.text.toLowerCase()}` : (b.status_label ?? (STATUS_LABEL[b.status] || b.status || '')),
    status_kind: noModel ? 'attention' : startFailed ? 'danger' : providerError ? providerError.kind : (b.status_kind ?? (STATUS_KIND[b.status] || 'neutral')),
    summary: noModel ? 'Выберите модель в настройках бота'
      : startFailed ? 'Компьютер не запустился: пересоздайте его в треде или в настройках бота'
        : (b.summary ?? (b.role || '')),
    restart_note: b.need_restart ? 'Перезапустится после текущей задачи' : '',
  };
}
async function listBotsView() {
  const [bots, providers] = await Promise.all([api.listBots(), api.listProviders({ quiet: true }).catch(() => null)]);
  if (providers) providerById = new Map(providers.map((p) => [p.id, p]));
  return bots.map(botView);
}

// ---------------------------------------------------------------------------
// Роутер
// ---------------------------------------------------------------------------
function parseHash() {
  const h = location.hash.replace(/^#/, '') || '/';
  const [path, query] = h.split('?');
  const parts = path.split('/').filter(Boolean);
  const qs = new URLSearchParams(query || '');
  if (parts.length === 0) return { name: 'main', qs };
  if (parts[0] === 'invite') return { name: 'invite', token: safeDecode(parts[1] || ''), qs };
  if (parts[0] === 'setup') return { name: 'setup', qs };
  if (parts[0] === 'welcome') return { name: 'welcome', qs };
  if (parts[0] === 'settings') return { name: 'settings', section: parts[1] || null, sub: parts[2] || null, action: parts[3] || null, qs };
  if (parts[0] === 'threads' && parts[1] && parts[2] === 'handoff') return { name: 'handoff', id: parts[1], qs };
  if (parts[0] === 'threads' && parts[1]) return { name: 'thread', id: parts[1], qs };
  if (parts[0] === 'bots' && parts[1] === 'new') return { name: 'bot-new', qs };
  if (parts[0] === 'bots' && parts[1] && parts[2] === 'browser') return { name: 'browser', id: parts[1], qs };
  if (parts[0] === 'bots' && parts[1]) return { name: 'bot-settings', id: parts[1], qs };
  if (parts[0] === 'approvals') return { name: 'approvals', id: parts[1] || null, qs };
  if (parts[0] === 'routines' && parts[1]) return { name: 'schedule', id: safeDecode(parts[1]), qs };
  if (parts[0] === 'routines') return { name: 'routines', qs };
  if (parts[0] === 'procedures') return { name: parts[1] ? 'procedure' : 'procedures', id: safeDecode(parts[1] || ''), qs };
  if (parts[0] === 'procedure-runs' && parts[1]) return { name: 'procedure-run', id: safeDecode(parts[1]), qs };
  if (parts[0] === 'incidents' && parts[1]) return { name: 'thread', id: parts[1], qs };
  if (parts[0] === 'usage') return { name: 'usage', qs };
  if (parts[0] === 'memory') return { name: 'memory', qs };
  if (parts[0] === 'activity') return { name: 'activity', qs };
  return { name: 'main', qs };
}
// fetch без ответа: Chrome пишет «Failed to fetch», Safari «Load failed», Firefox «NetworkError…».
function isNetworkFailure(err) {
  return err instanceof TypeError && /fetch|network|load failed/i.test(err.message || '');
}
function safeDecode(value) {
  try { return decodeURIComponent(value); } catch { return value; }
}

// Отрисовки идут наперегонки (render ниже), поэтому cleanup'ы копятся списком: устаревшая отрисовка не затирает cleanup
// новой, следующая смена экрана закрывает всё, что открыли обе.
let cleanups = [];
function setCleanup(fn) { cleanups.push(fn); }

const DESKTOP_QUERY = '(min-width: 1024px)';

// Пока новый экран грузится, прежний остаётся на месте, но не принимает клики: иначе действие уйдёт в уже ушедший экран
// (смена модели «не того» бота), а результат пропадёт вместе с его разметкой.
let renderSeq = 0;
let renderDone = 0; // номер последней завершившейся отрисовки
async function render() {
  for (const fn of cleanups.splice(0)) { try { fn(); } catch { /* noop */ } }
  const mine = ++renderSeq;
  app.inert = true;
  let route = parseHash();
  try {
    // Инвайт и первичная настройка работают без сессии.
    if (route.name === 'invite') { await account.viewInvite(route.token); return; }
    if (route.name === 'setup') { account.viewSetup(); return; }
    if (!session.user && !(await loadSession())) return;
    const desktop = window.matchMedia(DESKTOP_QUERY).matches;
    if (route.name === 'welcome') {
      // Приветствие только сразу после регистрации; при обновлении или прямом заходе открываем список ботов.
      if (account.takeWelcome()) { await account.viewWelcome(); return; }
      history.replaceState(null, '', `${location.pathname}${location.search}#/`);
      route = parseHash();
    }
    if (route.name === 'settings') { await viewSettingsRoute(route); return; }
    if (['procedures', 'procedure', 'procedure-run'].includes(route.name)) { await viewProcedureRoute(route); return; }
    if (route.name === 'memory') { await viewMemory(); return; }
    if (route.name === 'activity') { await viewActivity(); return; }
    // Новый бот: полноэкранный флоу описание → черновик, одинаков на телефоне и Mac.
    if (route.name === 'bot-new') { await viewBotNew(route.qs); return; }
    // Старый адрес экрана #/threads/<id>/handoff ведёт на экран браузера бота этого треда.
    if (route.name === 'handoff') {
      const thread = await api.getThread(route.id).catch(() => null);
      history.replaceState(null, '', `${location.pathname}${location.search}${thread ? `#/bots/${encodeURIComponent(thread.bot_id)}/browser` : '#/'}`);
      route = parseHash();
    }
    if (desktop) await renderDesktop(route);
    else await renderMobile(route);
  } catch (err) {
    if (err && err.status === 401) return; // сессия закрыта: событие bothub-unauthorized покажет вход
    if (isNetworkFailure(err)) { account.viewOffline(); return; }
    app.innerHTML = `<div class="center-screen"><p class="t-body">Не получилось загрузить экран.</p><p class="t-footnote">${esc(err.message || err)}</p><button class="btn btn-secondary" data-nav="#/">На главную</button></div>`;
  } finally {
    // Экраны пишут в app после своих запросов: устаревшая отрисовка, которая закончилась позже новой, затёрла бы её
    // (ушли из треда в список, а тред догрузился и вернулся на экран). Тогда актуальный экран рисуется заново.
    const overwroteNewer = mine !== renderSeq && renderDone > mine;
    renderDone = Math.max(renderDone, mine);
    if (mine === renderSeq) {
      app.inert = false;
      armStartWatch(mine);
    } else if (overwroteNewer) {
      render();
    }
  }
}

async function viewSettingsRoute({ section, sub, action }) {
  if (section === 'users') return account.viewUsers();
  if (section === 'password') return account.viewPassword();
  if (section === 'sessions') return account.viewSessions();
  if (section === 'providers') return viewProviders(sub, action);
  if (section === 'provider-requests') return viewProviderRequests();
  if (section === 'activity') return viewActivity();
  if (section === 'permissions') return account.viewStub(section);
  return account.viewSettings();
}

window.addEventListener('hashchange', render);
// Перерисовываем только при смене раскладки телефон/Mac: иначе клавиатура или поворот сбрасывали бы введённые в форму данные.
let lastDesktop = window.matchMedia(DESKTOP_QUERY).matches;
window.addEventListener('resize', debounce(() => {
  const now = window.matchMedia(DESKTOP_QUERY).matches;
  if (now === lastDesktop) return;
  lastDesktop = now;
  render();
}, 200));
function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

// iOS сдвигает страницу, чтобы поднять сфокусированный инпут над клавиатурой,
// а после blur не всегда возвращает её сама (виден пустой сдвиг снизу). Окно
// у нас в принципе не скроллится (html/body: overflow:hidden) — сдвигаться
// может только внутренняя область треда, её и возвращаем к низу (к последнему
// сообщению), а не трогаем window.
app.addEventListener('focusout', (e) => {
  if (e.target.id !== 'composer-input') return;
  const scroller = e.target.closest('.screen, .desktop-shell')?.querySelector('.thread-body, .desktop-thread-body');
  if (!scroller) return;
  setTimeout(() => { scroller.scrollTop = scroller.scrollHeight; }, 50);
});

// Делегирование кликов по data-action
app.addEventListener('click', async (e) => {
  if (e.target.closest('[data-skip]')) {
    e.preventDefault(); // адрес с #content ломал бы хэш-роутер
    document.getElementById('content')?.focus();
    return;
  }
  const nav = e.target.closest('[data-nav]');
  if (nav) { location.hash = nav.getAttribute('data-nav'); return; }
  const act = e.target.closest('[data-action]');
  if (!act) return;
  const action = act.getAttribute('data-action');
  await handleAction(action, act);
});

// Порог автосжатия: число рядом с ползунком меняется на лету, сохраняется отпущенное значение.
app.addEventListener('input', (e) => {
  if (e.target.id !== 'ac-range') return;
  const out = e.target.closest('.ac-card')?.querySelector('[data-ac-value]');
  if (out) out.textContent = `${e.target.value}%`;
});
app.addEventListener('change', (e) => {
  if (e.target.id !== 'ac-range') return;
  updateBotSetting('set-autocompact-percent', e.target);
});

app.addEventListener('keydown', (e) => {
  if (e.target.id !== 'composer-input') return;
  const desktop = window.matchMedia('(min-width: 1024px)').matches;
  const trigger = desktop ? (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) : false;
  if (trigger) {
    e.preventDefault();
    sendMessage(e.target.getAttribute('data-thread'), e.target);
  }
});

async function sendMessage(threadId, input) {
  const el = input || document.getElementById('composer-input');
  const text = el && el.value.trim();
  if (!text) return;
  el.value = '';
  const turn = await api.createTurn(threadId, text, 'iphone');
  if (activeCtx && turn && turn.id != null) activeCtx.turnStarted(turn.id);
}

async function handleAction(action, el) {
  if (action === 'thread-approval') {
    const id = el.getAttribute('data-id');
    const decision = el.getAttribute('data-decision');
    const card = el.closest('[data-approval-id]');
    const actions = card && card.querySelector('[data-approval-actions]');
    if (!id || !card || !actions) return;
    actions.querySelectorAll('button').forEach((button) => { button.disabled = true; });
    try {
      const client = window.matchMedia('(min-width: 1024px)').matches ? 'mac' : 'iphone';
      await api.decideApproval(id, decision, false, client);
      applyApprovalDecision(card, decision);
    } catch (error) {
      actions.querySelectorAll('button').forEach((button) => { button.disabled = false; });
      throw error;
    }
  } else if (action === 'approve' || action === 'reject') {
    const id = el.getAttribute('data-id');
    const remember = !!document.querySelector(`[data-remember="${id}"]`)?.checked;
    el.disabled = true;
    await api.decideApproval(id, action === 'approve' ? 'approve' : 'reject', remember);
    render();
  } else if (action === 'run-schedule') {
    el.disabled = true;
    await api.runSchedule(el.getAttribute('data-id'));
    render();
  } else if (action === 'toggle-schedule') {
    const id = el.getAttribute('data-id');
    const list = await api.listSchedules();
    const s = list.find((x) => x.id === id);
    await api.patchSchedule(id, { enabled: !(s && s.enabled) });
    render();
  } else if (action === 'toggle-catch-up') {
    // Переключатель меняется сразу; при ошибке возвращается как был, а сбой уходит в общий обработчик.
    const wanted = el.checked;
    try { await api.patchSchedule(el.getAttribute('data-id'), { catch_up: wanted }); } catch (error) { el.checked = !wanted; throw error; }
  } else if (action === 'send-message') {
    await sendMessage(el.getAttribute('data-thread'));
  } else if (action === 'stop-turn') {
    await api.stopTurn(el.getAttribute('data-turn') || 'current');
  } else if (action === 'pick-avatar' || action === 'set-executor' || action === 'toggle-mfc' || action === 'toggle-autocompact') {
    await updateBotSetting(action, el);
  } else if (action === 'recreate-bot') {
    await recreateBotAction(el);
  } else if (action === 'delete-bot') {
    confirmDeleteBot({ id: el.getAttribute('data-bot'), name: el.getAttribute('data-name') });
  } else if (action === 'save-procedure') {
    openSaveProcedureDialog({ threadId: el.getAttribute('data-thread'), turnId: el.getAttribute('data-turn') });
  } else if (action === 'set-usage-days') {
    const days = parseInt(el.getAttribute('data-days'), 10) || 7;
    currentUsageDays = days;
    render();
  } else if (action === 'retry-usage') {
    render();
  } else if (action === 'open-thread') {
    // Реальное ядро не выдаёт предсказуемых id тредов — ищем последний тред бота,
    // а если его ещё нет (первый разговор), создаём.
    const botId = el.getAttribute('data-bot');
    const thread = await api.getThreadByBot(botId) || await api.createThread(botId);
    location.hash = `#/threads/${thread.id}`;
  }
}

// Настройки бота меняются на месте: экран не перерисовывается, фокус остаётся на нажатом элементе.
// Сначала показываем результат сразу, потом сверяем с ответом сервера; при ошибке возвращаем как было.
const botState = new Map(); // id бота -> последнее подтверждённое состояние
const botSeq = new Map(); // id бота -> номер последнего запроса: устаревшие ответы не применяем

function paintBotSettings(scope, bot) {
  scope.querySelectorAll('[data-action="set-executor"]').forEach((b) => b.setAttribute('aria-checked', String(b.getAttribute('data-value') === bot.executor)));
  scope.querySelectorAll('[data-action="pick-avatar"]').forEach((b) => b.setAttribute('aria-pressed', String(b.getAttribute('data-avatar') === bot.avatar)));
  const label = scope.querySelector('[data-model-label]');
  if (label) label.textContent = bot.model ? modelTitle(bot.model) : 'модель не задана';
  const slot = scope.querySelector('[data-bot-state-slot]');
  if (slot) slot.innerHTML = botStateBanner(bot);
  const mfc = scope.querySelector('#mfc');
  if (mfc) mfc.checked = !!bot.mac_full_control;
  const acSwitch = scope.querySelector('#ac-switch');
  const acRange = scope.querySelector('#ac-range');
  if (acSwitch && acRange) {
    const on = bot.auto_compact_percent !== null;
    acSwitch.checked = on;
    acRange.disabled = !on;
    // Выключено (null): ползунок остаётся на прежнем значении, оно вернётся при включении.
    if (Number.isInteger(bot.auto_compact_percent)) acRange.value = String(bot.auto_compact_percent);
    const out = scope.querySelector('[data-ac-value]');
    if (out) out.textContent = `${acRange.value}%`;
  }
  document.querySelectorAll(`[data-avatar-slot="${CSS.escape(bot.id)}"]`).forEach((slot) => {
    slot.innerHTML = avatarHtml(bot.avatar || 'robot', bot.provider, Number(slot.getAttribute('data-size')) || 44);
  });
}

async function updateBotSetting(action, el) {
  const scope = el.closest('[data-bot]');
  const botId = scope?.getAttribute('data-bot');
  if (!botId) return; // новый бот ещё не создан, нечего патчить
  const before = botState.get(botId);
  if (!before) return;
  const patch = action === 'pick-avatar' ? { avatar: el.getAttribute('data-avatar') }
    : action === 'set-executor' ? { executor: el.getAttribute('data-value') }
    : action === 'toggle-autocompact' ? { auto_compact_percent: el.checked ? Number(scope.querySelector('#ac-range').value) : null }
    : action === 'set-autocompact-percent' ? { auto_compact_percent: Number(el.value) }
    : { mac_full_control: el.checked };
  const alertBox = scope.querySelector('#bot-alert');
  const seq = (botSeq.get(botId) || 0) + 1;
  botSeq.set(botId, seq);
  if (alertBox) alertBox.innerHTML = '';
  paintBotSettings(scope, { ...before, ...patch });
  try {
    const row = await api.patchBot(botId, patch);
    const saved = botView({ ...before, status_label: row.status_label, status_kind: row.status_kind, summary: row.summary, ...row });
    botState.set(botId, saved);
    if (botSeq.get(botId) === seq) paintBotSettings(scope, saved);
  } catch (err) {
    if (botSeq.get(botId) !== seq) return;
    paintBotSettings(scope, before);
    if (alertBox) {
      alertBox.innerHTML = alertHtml('Изменение не сохранено', err && err.status ? 'Попробуйте ещё раз через минуту.' : 'Сервер не отвечает. Проверьте сеть или VPN.');
      alertBox.firstElementChild.focus();
    }
  }
}

// ---------------------------------------------------------------------------
// Сессия: вход, выход, очистка данных пользователя
// ---------------------------------------------------------------------------
const session = { user: null };
const UID_KEY = 'bothub_uid';
const SHELL_CACHE_PREFIX = 'bothub-shell';

function lsGet(key) { try { return localStorage.getItem(key); } catch { return null; } }
function lsSet(key, value) { try { localStorage.setItem(key, value); } catch { /* память недоступна */ } }
function lsRemove(key) { try { localStorage.removeItem(key); } catch { /* память недоступна */ } }

// Кэши данных (всё, кроме оболочки приложения) и локальные заметки прошлого пользователя.
// Ответы /api/* service worker не кэширует вовсе, это вторая линия защиты.
async function clearUserData() {
  lsRemove('bothub_invite_notes');
  try {
    if ('caches' in window) {
      const keys = await caches.keys();
      await Promise.all(keys.filter((k) => !k.startsWith(SHELL_CACHE_PREFIX)).map((k) => caches.delete(k)));
    }
  } catch { /* кэши недоступны */ }
  try {
    if (navigator.serviceWorker && navigator.serviceWorker.controller) navigator.serviceWorker.controller.postMessage({ type: 'clear-data' });
  } catch { /* нет service worker */ }
}

function signedIn(user) {
  const previous = lsGet(UID_KEY);
  if (previous && previous !== user.id) clearUserData(); // смена пользователя: чужие данные не показываем
  lsSet(UID_KEY, user.id);
  session.user = user;
}

async function endSession() {
  session.user = null;
  api.forgetCsrf();
  lsRemove(UID_KEY);
  await clearUserData();
}

async function signOut() {
  try { await api.logout(); } catch { /* сессия уже закрыта на сервере */ }
  await endSession();
  history.replaceState(null, '', `${location.pathname}${location.search}#/`);
  render();
}

function showLogin(options) {
  history.replaceState(null, '', `${location.pathname}${location.search}#/`);
  account.viewLogin(options);
}

// После входа остаёмся на исходном маршруте (хэш не меняли), либо идём на заданный экран.
function afterLogin(target) {
  if (target && location.hash !== target) location.hash = target;
  else render();
}

async function loadSession() {
  try {
    signedIn(await api.authMe());
    return true;
  } catch (err) {
    if (err.status === 401) {
      api.setToken(''); // устаревший или неверный OWNER_TOKEN
      if (await api.needsSetup()) account.viewSetup();
      else { account.viewLogin(); }
    } else if (!err.status) {
      account.viewOffline(); // нет связи: оболочка открылась, данных нет
    } else {
      account.viewLogin({ initialError: 'server' });
    }
    return false;
  }
}

window.addEventListener('bothub-unauthorized', () => {
  if (!session.user) return;
  session.user = null;
  api.forgetCsrf();
  render();
});

// ---------------------------------------------------------------------------
// Каркас экранов разделов: на телефоне шапка и нижняя панель действий, на Mac сайдбар и правая колонка
// ---------------------------------------------------------------------------
// Аватар, который можно обновить на месте (настройки бота): слот помнит id бота и размер.
function avatarSlot(bot, size) {
  return `<span class="avatar-slot" data-avatar-slot="${esc(bot.id)}" data-size="${size}">${avatarHtml(bot.avatar || 'robot', bot.provider, size)}</span>`;
}

const SKIP_LINK = '<a class="skip-link" href="#content" data-skip>К содержимому</a>';

function desktopSidebar({ bots, approvals, activeBotId = null, activeNav = '' }) {
  const link = (key, href, iconHtml, label) => `<a href="${href}" class="desktop-nav-link"${key === activeNav ? ' aria-current="page"' : ''}>${iconHtml}${label}</a>`;
  return `<aside class="desktop-sidebar">
    <div class="desktop-sidebar-title">botstead</div>
    ${bots.map((b) => `<a href="javascript:void(0)" data-action="open-thread" data-bot="${esc(b.id)}" class="desktop-bot-row ${b.id === activeBotId ? 'active' : ''}">
      ${avatarSlot(b, 32)}
      <span class="stack min-w-0"><span class="t-callout" style="font-size:14px;" data-i18n-skip>${esc(b.name)}</span><span class="t-footnote" style="font-size:12px;">${esc(b.status_label)}</span></span>
    </a>`).join('')}
    <div style="height:12px;"></div>
    ${link('routines', '#/routines', ICONS.routines, 'Рутины')}
    ${link('approvals', '#/approvals', ICONS.approvals, `Решения${approvals.length ? ' · ' + approvals.length : ''}`)}
    ${link('memory', '#/memory', ICONS.memory, 'Память')}
    ${link('usage', '#/usage', ICONS.usage, 'Расход')}
    ${link('activity', '#/activity', ICONS.activity, 'Активность')}
    ${link('settings', '#/settings', ICONS.settings, 'Настройки')}
  </aside>`;
}

async function frame({ title, subtitle = '', backHref = null, body, mobileActions = '', desktopAside = '', activeNav = 'settings' }) {
  if (!window.matchMedia(DESKTOP_QUERY).matches) {
    app.innerHTML = `<div class="screen">
      ${backHeader({ title, subtitle, backHref: backHref || '#/' })}
      <div class="thread-body">${body}</div>
      ${mobileActions ? `<div class="action-bar">${mobileActions}</div>` : ''}
    </div>`;
    return;
  }
  const [bots, approvals] = await Promise.all([listBotsView(), api.listApprovals('pending')]);
  app.innerHTML = `<div class="desktop-shell">
    ${SKIP_LINK}
    ${desktopSidebar({ bots, approvals, activeNav })}
    <main class="desktop-main" id="content" tabindex="-1">
      <div class="desktop-thread-head">
        ${backHref && backHref !== '#/' ? `<a href="${backHref}" aria-label="Назад" class="icon-btn">${ICONS.back}</a>` : ''}
        <div class="flex-1 min-w-0 stack"><h1 class="t-headline header-title">${esc(title)}</h1>${subtitle ? `<span class="t-footnote">${esc(subtitle)}</span>` : ''}</div>
      </div>
      <div class="desktop-thread-body">${body}</div>
    </main>
    ${desktopAside ? `<aside class="desktop-aside">${desktopAside}</aside>` : ''}
  </div>`;
}

const account = createAccount({
  app,
  session,
  frame,
  signedIn,
  signOut,
  endSession,
  showLogin,
  afterLogin,
  rerender: () => render(),
  setCleanup,
});

// ---------------------------------------------------------------------------
// TabBar / Header
// ---------------------------------------------------------------------------
function tabBar(active, pendingCount) {
  const tabs = [
    { key: 'main', href: '#/', label: 'Боты', icon: ICONS.bots },
    { key: 'routines', href: '#/routines', label: 'Рутины', icon: ICONS.routines },
    { key: 'approvals', href: '#/approvals', label: 'Решения', icon: ICONS.approvals, dot: pendingCount > 0 },
    { key: 'usage', href: '#/usage', label: 'Расход', icon: ICONS.usage },
  ];
  return `<nav aria-label="Разделы" class="tabbar">${tabs.map((t) => `
    <a href="${t.href}" class="tab-item ${t.key === active ? 'active' : ''}" ${t.key === active ? 'aria-current="page"' : ''}>
      ${t.icon}${esc(t.label)}${t.dot ? `<span class="tab-dot" aria-label="есть ожидающие"></span>` : ''}
    </a>`).join('')}
  </nav>`;
}

// ---------------------------------------------------------------------------
// Main: Боты
// ---------------------------------------------------------------------------
async function viewMain() {
  const [bots, approvals, mac] = await Promise.all([listBotsView(), api.listApprovals('pending'), api.macStatus()]);
  const macRow = `<a href="#/threads/t-mac/handoff" class="bot-card" style="align-items:center;">
    <span style="display:flex;color:var(--fg-default);">${ICONS.laptop}</span>
    <span class="flex-1 stack">
      <span class="t-headline" style="font-size:15px;">${esc(mac.info?.host || 'Mac')}</span>
      <span class="t-footnote">${macStatusLine(mac)}</span>
    </span>
    <span class="status-line t-footnote" style="color:var(--fg-default);">
      <span class="status-dot ${mac.state === 'online' ? 'success' : 'neutral'}"></span>${mac.state === 'online' ? 'В сети' : 'Не в сети'}
    </span>
  </a>`;
  const banner = approvals.length ? `<a href="#/approvals" class="banner-attention">
    ${ICONS.alert}<span class="flex-1">${approvals.length} действи${approvals.length === 1 ? 'е' : 'я'} жд${approvals.length === 1 ? 'ёт' : 'ут'} решения</span>
    ${icon('<path d="M9 6l6 6-6 6"></path>', 16, 2.2)}
  </a>` : '';
  const cards = bots.map((b) => botCard(b)).join('');
  app.innerHTML = `<div class="screen">
    <div class="root-header"><h1 class="h-large-title">Боты</h1>
      <span class="row gap-2">
        <a href="#/memory" aria-label="Память" class="icon-btn sunken">${ICONS.memory}</a>
        <a href="#/settings" aria-label="Настройки" class="icon-btn sunken">${ICONS.settings}</a>
        <a href="#/bots/new" class="btn btn-primary">${ICONS.plus}Бот</a>
      </span>
    </div>
    <div class="bot-list">${macRow}${banner}${cards}</div>
    ${tabBar('main', approvals.length)}
  </div>`;
}

function statusLineHtml(b) {
  return `<span class="status-dot ${statusDotClass(b.status_kind)}"></span>${esc(b.status_label)}`;
}

// Бот в starting запускается фоном (docs/contracts.md §9): пока такой бот есть на экране, список ботов раз в START_POLL_MS
// перечитывается и экран обновляется на месте (баннер и подсказка в треде, строка статуса в списке). Экран не перерисовывается
// и не ждёт ответа, поэтому render() и фокус не страдают; смена экрана (renderSeq) останавливает опрос.
const START_POLL_MS = 2000;
let startWatchTimer = 0;
function armStartWatch(seq) {
  clearTimeout(startWatchTimer);
  if (!document.querySelector('[data-bot-state="starting"], .bot-card[data-bot-status="starting"]')) return;
  startWatchTimer = setTimeout(async () => {
    if (seq !== renderSeq) return;
    try {
      const bots = await listBotsView();
      if (seq === renderSeq) paintStartedBots(bots);
    } catch { /* сеть: следующий круг */ }
    if (seq === renderSeq) armStartWatch(seq);
  }, START_POLL_MS);
}
function paintStartedBots(bots) {
  for (const b of bots) {
    document.querySelectorAll(`.bot-card[data-bot-status="starting"] [data-bot="${CSS.escape(b.id)}"]`).forEach((link) => {
      const card = link.closest('.bot-card');
      if (b.status === 'starting' || !card) return;
      card.setAttribute('data-bot-status', b.status || '');
      const line = card.querySelector('.status-line');
      if (line) line.innerHTML = statusLineHtml(b);
    });
    document.querySelectorAll(`[data-bot-state="starting"][data-bot="${CSS.escape(b.id)}"]`).forEach((banner) => {
      if (b.status === 'starting') return;
      const holder = banner.closest('.thread-state');
      if (holder) holder.innerHTML = botStateBanner(b);
      document.querySelectorAll('[data-bot-hint-slot]').forEach((slot) => { slot.innerHTML = ''; });
    });
  }
}

// Карточка: основная часть открывает тред, отдельная иконка справа ведёт в настройки бота (не вложенные ссылки).
function botCard(b) {
  const badge = b.noModel ? '<span class="badge badge-attention">Нет модели</span>' : `<span class="badge ${badgeClass(b.provider)}">${esc(modelTitle(b.model))}</span>`;
  return `<div class="bot-card" data-bot-status="${esc(b.status || '')}">
    <a href="javascript:void(0)" data-action="open-thread" data-bot="${esc(b.id)}" class="bot-card-main">
      ${avatarHtml(b.avatar, b.provider, 44)}
      <span class="bot-card-body">
        <span class="bot-card-name-row">
          <span class="t-headline" data-i18n-skip>${esc(b.name)}</span>
          ${badge}
        </span>
        <span class="bot-card-summary" data-i18n-skip>${esc(b.summary)}</span>
        <span class="bot-card-meta">
          <span class="status-line">${statusLineHtml(b)}</span>
          <span data-i18n-skip>${esc(b.location)}</span>
          ${b.restart_note ? `<span>${esc(b.restart_note)}</span>` : ''}
        </span>
      </span>
    </a>
    <div class="bot-card-tools">
      <a href="#/bots/${esc(b.id)}" class="icon-btn sunken bot-card-settings" aria-label="Настройки бота: ${esc(b.name)}">${ICONS.sliders}</a>
      ${hasBrowser(b) ? `<a href="#/bots/${esc(b.id)}/browser" class="icon-btn sunken bot-card-screen" aria-label="Экран бота: ${esc(b.name)}">${ICONS.screen}</a>` : ''}
    </div>
  </div>`;
}

// ---------------------------------------------------------------------------
// Thread
// ---------------------------------------------------------------------------
function planStepIcon(status) {
  if (status === 'done') return `<span style="color:var(--success-fg);">${ICONS.check}</span>`;
  if (status === 'error') return `<span style="color:var(--danger-fg);">${icon('<path d="M6 6l12 12M18 6L6 18"></path>', 16, 2.4)}</span>`;
  if (status === 'running') return `<span class="spin" style="color:var(--fg-default);">${icon('<circle cx="12" cy="12" r="8" stroke-dasharray="34 50"></circle>', 16, 2.4)}</span>`;
  if (status === 'waiting_approval') return `<span style="color:var(--attention-fg);">${ICONS.alert}</span>`;
  return `<span style="color:var(--fg-muted);">${icon('<circle cx="12" cy="12" r="3"></circle>', 16, 2.4)}</span>`;
}

function renderPlan(payload) {
  const steps = payload.steps || [];
  const done = steps.filter((s) => s.status === 'done').length;
  return `<ol class="plan-list card card-pad">${steps.map((s) => `
    <li class="plan-step ${esc(s.status)}"><span class="plan-step-icon">${planStepIcon(s.status)}</span><span>${esc(s.title)}${s.error ? `<br><span class="t-footnote" style="color:var(--danger-fg);">${esc(s.error)}</span>` : ''}</span></li>`).join('')}
  </ol><div class="t-footnote">${done} из ${steps.length} шагов сделано</div>`;
}

function usageMetaHtml(payload) {
  return `<div class="msg-meta"><span>${fmtTokens((payload.tokens_in || 0) + (payload.tokens_out || 0))}</span><span>${payload.seconds ?? '–'} с</span><span>${esc(payload.model || '')}</span></div>`;
}

function approvalCardHtml(payload, decision) {
  const id = payload.approval_id || '';
  const resolved = decision === 'approve' || decision === 'reject';
  return `<section class="risk-card" data-approval-id="${esc(id)}" style="background:var(--attention-bg);border-color:var(--attention-border);">
    <div class="risk-tag" style="color:var(--attention-text);">${ICONS.alert}${esc(RISK_LABEL[payload.risk] || 'ПОДТВЕРЖДЕНИЕ')}</div>
    <div class="t-callout" style="font-size:16px;font-weight:500;">${esc(payload.title || '')}</div>
    <div class="approval-status t-footnote" role="status" data-approval-status ${resolved ? '' : 'hidden'}>${decision === 'approve' ? 'Разрешено' : decision === 'reject' ? 'Отклонено' : ''}</div>
    ${resolved ? '' : `<div class="btn-row btn-row-2" data-approval-actions>
      <button type="button" class="btn btn-secondary" data-action="thread-approval" data-decision="reject" data-id="${esc(id)}">Отклонить</button>
      <button type="button" class="btn btn-attention" data-action="thread-approval" data-decision="approve" data-id="${esc(id)}">Разрешить</button>
    </div>`}
  </section>`;
}

function applyApprovalDecision(card, decision) {
  card.querySelector('[data-approval-actions]')?.remove();
  const status = card.querySelector('[data-approval-status]');
  if (status) {
    status.hidden = false;
    status.textContent = decision === 'approve' ? 'Разрешено' : 'Отклонено';
  }
}

function renderEvent(ev, botAvatar) {
  switch (ev.kind) {
    case 'system':
      return `<div class="system-pill">${esc(ev.payload.text)}</div>`;
    case 'user_msg':
      return `<div class="msg-user" data-i18n-skip>${esc(ev.payload.text)}</div>`;
    case 'assistant_msg':
      return `<div class="msg-bot"><span class="msg-bot-text" data-i18n-skip>${esc(ev.payload.text)}${ev.payload.final === false ? '<span class="t-muted">…</span>' : ''}</span></div>`;
    case 'usage':
      return usageMetaHtml(ev.payload);
    case 'plan':
      return renderPlan(ev.payload);
    case 'file':
      return renderFileCard(ev.payload);
    case 'approval_req':
      return approvalCardHtml(ev.payload, null);
    case 'guard':
      return `<div class="risk-card" style="background:var(--danger-bg);border-color:transparent;">
        <div class="risk-tag" style="color:var(--danger-fg);">${ICONS.alert}ПРЕДОХРАНИТЕЛЬ</div>
        <div class="t-body">${esc(ev.payload.detail)}</div>
      </div>`;
    case 'interrupted':
      return `<div class="system-pill">Остановлено: сохранено ${(ev.payload.saved || []).length}, отменено ${(ev.payload.cancelled || []).length}</div>`;
    case 'compacted':
      return ctxCompactedHtml(ev.payload || {});
    case 'compact_failed':
      return `<div class="ctx-divider" data-ctx-event="compact_failed"><span>Не удалось сжать контекст${ev.payload && ev.payload.detail ? `<span data-i18n-skip>: ${esc(ev.payload.detail)}</span>` : ''}</span></div>`;
    case 'auto_compact_disabled':
      return '<div class="ctx-divider" data-ctx-event="auto_compact_disabled"><span>Автосжатие отключено: сжатие почти не помогло</span></div>';
    default:
      return '';
  }
}

// Завершённый turn с действиями браузера (browser_step, кроме чтения страницы) получает под ответом кнопку
// «Сохранить как процедуру». Завершённость: событие status со статусом done или usage с turn_id; отказ или остановка убирают кнопку.
const READ_ONLY_STEPS = ['snapshot', 'screenshot'];
function createThreadEventHandler(body, botAvatar, approvalDecisions, threadId, ctl = null) {
  const assistantEvents = new Map();
  const actionTurns = new Set();
  const finishedTurns = new Set();
  const findOffer = (turnId) => Array.from(body.querySelectorAll('[data-offer-turn]')).find((el) => el.dataset.offerTurn === turnId);
  function offerProcedure(turnId) {
    if (!turnId || !actionTurns.has(turnId) || !finishedTurns.has(turnId) || findOffer(turnId)) return;
    body.insertAdjacentHTML('beforeend', `<div class="proc-offer" data-offer-turn="${esc(turnId)}"><button type="button" class="btn btn-secondary" data-action="save-procedure" data-turn="${esc(turnId)}" data-thread="${esc(threadId)}">${ICONS.checklist}Сохранить как процедуру</button></div>`);
  }
  const pendingUsage = new Map();
  const findBubble = (turnId) => Array.from(body.querySelectorAll('.msg-bot[data-turn-id]'))
    .find((bubble) => bubble.dataset.turnId === turnId);

  function setUsage(bubble, payload) {
    bubble.querySelector('.msg-meta')?.remove();
    bubble.insertAdjacentHTML('beforeend', usageMetaHtml(payload));
  }

  function updateAssistantBubble(turnId) {
    const events = assistantEvents.get(turnId).sort((a, b) => a.seq - b.seq);
    const finalEvents = events.filter((event) => event.final);
    const lastFinal = finalEvents[finalEvents.length - 1];
    const text = lastFinal && lastFinal.text
      ? lastFinal.text
      : events.map((event) => event.text).filter(Boolean).join('');
    const isFinal = finalEvents.length > 0;
    let bubble = findBubble(turnId);

    if (isFinal && !text) {
      bubble?.remove();
      return;
    }
    if (!bubble) {
      body.insertAdjacentHTML('beforeend', `<div class="msg-bot" data-turn-id="${esc(turnId)}"><span class="msg-bot-text" data-i18n-skip></span></div>`);
      bubble = findBubble(turnId);
    }
    bubble.querySelector('.msg-bot-text').innerHTML = `${esc(text)}${isFinal ? '' : '<span class="t-muted">…</span>'}`;
    if (pendingUsage.has(turnId)) {
      setUsage(bubble, pendingUsage.get(turnId));
      pendingUsage.delete(turnId);
    }
  }

  return (ev) => {
    const payload = ev.payload || {};
    const turnKey = ev.turn_id != null && ev.turn_id !== '' ? String(ev.turn_id) : '';
    if (ev.kind === 'browser_step') {
      if (turnKey && !READ_ONLY_STEPS.includes(payload.action)) { actionTurns.add(turnKey); offerProcedure(turnKey); }
      return;
    }
    if (ev.kind === 'status' && payload.turn_id != null && payload.turn_id !== '') {
      const key = String(payload.turn_id);
      if (payload.status === 'done') { finishedTurns.add(key); offerProcedure(key); } else if (['error', 'stopped', 'failed'].includes(payload.status)) { finishedTurns.delete(key); findOffer(key)?.remove(); }
    }
    if (ev.kind === 'usage' && turnKey) { finishedTurns.add(turnKey); offerProcedure(turnKey); }
    // Ход сжатия невидим: ни пузыря ответа, ни строки расхода (turn_id приходит в событиях и в ответе POST /compact).
    if ((ev.kind === 'assistant_msg' || ev.kind === 'usage') && ctl && turnKey && ctl.isCompactTurn(turnKey)) return;
    if (ev.kind === 'assistant_msg') {
      if (ev.turn_id == null || ev.turn_id === '') {
        if (payload.text || payload.final !== true) body.insertAdjacentHTML('beforeend', renderEvent(ev, botAvatar));
        return;
      }
      const events = assistantEvents.get(ev.turn_id) || [];
      events.push({ seq: ev.seq || 0, text: typeof payload.text === 'string' ? payload.text : '', final: payload.final === true });
      assistantEvents.set(ev.turn_id, events);
      updateAssistantBubble(ev.turn_id);
      return;
    }
    if (ev.kind === 'usage') {
      if (ev.turn_id != null && ev.turn_id !== '') {
        const bubble = findBubble(ev.turn_id);
        if (bubble) setUsage(bubble, payload);
        else pendingUsage.set(ev.turn_id, payload);
      } else {
        const bubbles = body.querySelectorAll('.msg-bot');
        const last = bubbles[bubbles.length - 1];
        if (last) setUsage(last, payload);
        else body.insertAdjacentHTML('beforeend', usageMetaHtml(payload));
      }
      return;
    }
    if (ev.kind === 'approval_req') {
      body.insertAdjacentHTML('beforeend', approvalCardHtml(payload, approvalDecisions.get(payload.approval_id)));
      return;
    }
    if (ev.kind === 'approval_dec') {
      const id = payload.approval_id;
      approvalDecisions.set(id, payload.decision);
      const card = Array.from(body.querySelectorAll('[data-approval-id]'))
        .find((item) => item.dataset.approvalId === id);
      if (card) applyApprovalDecision(card, payload.decision);
      return;
    }
    body.insertAdjacentHTML('beforeend', renderEvent(ev, botAvatar));
  };
}

async function connectThreadStream(threadId, body, botAvatar, ctl = null) {
  let events = [];
  try { events = await api.getEvents(threadId); } catch { /* live tail read retries */ }
  events = (events || []).slice().sort((a, b) => a.seq - b.seq);
  const approvalDecisions = new Map(events
    .filter((ev) => ev.kind === 'approval_dec' && ev.payload?.approval_id)
    .map((ev) => [ev.payload.approval_id, ev.payload.decision]));
  const handleEvent = createThreadEventHandler(body, botAvatar, approvalDecisions, threadId, ctl);
  for (const ev of events) { if (ctl) ctl.onEvent(ev, false); handleEvent(ev); }
  body.scrollTop = body.scrollHeight;
  const since = events.reduce((last, ev) => Math.max(last, ev.seq || 0), 0);
  return openThreadStream(threadId, since, (ev) => {
    if (ctl) ctl.onEvent(ev, true);
    handleEvent(ev);
    body.scrollTop = body.scrollHeight;
  });
}

function renderFileCard(f) {
  const ext = (f.name.split('.').pop() || '').slice(0, 4).toUpperCase();
  return `<div class="file-card">
    <div class="file-card-head">
      <span class="file-ext">${esc(ext)}</span>
      <span class="file-meta">
        <span class="file-name" data-i18n-skip>${esc(f.name)}</span>
        <span class="file-path" data-i18n-skip>${esc(f.origin || '')}</span>
        <span class="t-footnote">${fmtBytes(f.size)}${f.modified ? ` · изменён ${esc(f.modified)}` : ''}</span>
      </span>
    </div>
    <div class="btn-row btn-row-3">
      <button type="button" class="btn btn-secondary">${ICONS.preview}Превью</button>
      <button type="button" class="btn btn-secondary">В тред</button>
      <button type="button" class="btn btn-secondary">${ICONS.download}Скачать</button>
    </div>
  </div>`;
}

// ---------------------------------------------------------------------------
// Контекст треда: индикатор заполнения в шапке, панель со сжатием и сводкой, вставки в ленте (этап 8)
// ---------------------------------------------------------------------------
const CTX_WARN = 70; // предупреждение от этого процента
const CTX_CRIT = 90; // критическое значение от этого процента
const CTX_RING = 69.115; // длина окружности кольца (r=11)
const CTX_DONE = ['done', 'error', 'stopped', 'failed'];
const CTX_TAB_STOP = 'a[href], button, input, select, textarea, summary, [contenteditable], [tabindex]:not([tabindex="-1"])';

let activeCtx = null; // контроллер контекста открытого треда: sendMessage сообщает ему о новом ходе

function ctxLevel(percent) {
  if (percent !== null && percent >= CTX_CRIT) return 'crit';
  if (percent !== null && percent >= CTX_WARN) return 'warn';
  return 'ok';
}
// Ответ GET /api/threads/{id}: context {tokens, window, percent, estimated, compacted_at, compactions}; процент считаем сами, если его нет.
function ctxView(context) {
  const c = context || {};
  const tokens = Number.isFinite(c.tokens) ? c.tokens : null;
  const win = Number.isFinite(c.window) && c.window > 0 ? c.window : null;
  let percent = null;
  if (tokens !== null && win !== null) percent = Math.max(0, Math.min(100, Math.floor(Number.isFinite(c.percent) ? c.percent : tokens * 100 / win)));
  return { tokens, window: win, percent, estimated: c.estimated === true, compactedAt: c.compacted_at || null };
}
function ctxShort(n) {
  return n >= 1000 ? `${Math.round(n / 1000)}k` : String(n);
}
function ctxRingHtml(percent) {
  const dash = percent ? (CTX_RING * percent / 100).toFixed(1) : null;
  return `<svg class="ctx-ring" viewBox="0 0 28 28" aria-hidden="true"><circle class="ctx-ring-track" cx="14" cy="14" r="11"></circle>${dash ? `<circle class="ctx-ring-fill" cx="14" cy="14" r="11" stroke-dasharray="${dash} ${CTX_RING}"></circle>` : ''}</svg>`;
}
// Подпись кнопки для скринридера: по-русски, как весь интерфейс; английская берётся из каталога pwa/i18n/en-app.js.
function ctxButtonParts(v) {
  if (v.percent !== null) return { text: `${v.percent}%`, label: `Контекст заполнен на ${v.percent}%` };
  if (v.tokens !== null) return { text: ctxShort(v.tokens), label: `Контекст: ${v.tokens} токенов` };
  return { text: '–', label: 'Контекст: нет данных' };
}
function ctxCompactedHtml(p) {
  const after = fmtTokens(p.tokens_after || 0);
  let text;
  if (p.tokens_before == null) text = p.auto === true ? `Контекст сжат автоматически, сейчас ${after}` : `Контекст сжат, сейчас ${after}`;
  else if (p.auto === true) text = `Контекст сжат автоматически: было ${fmtTokens(p.tokens_before)}, стало ${after}`;
  else text = `Контекст сжат: было ${fmtTokens(p.tokens_before)}, стало ${after}`;
  return `<div class="ctx-divider" data-ctx-event="compacted"><span>${text}</span></div>`;
}

// Разметка индикатора и панели: одна и та же в шапке телефона и Mac. Содержимое заполняет mountContext.
function ctxHtml() {
  return `<div class="ctx ctx-ok" data-ctx>
    <button type="button" class="ctx-btn" data-ctx-toggle aria-haspopup="dialog" aria-expanded="false" aria-controls="ctx-panel"></button>
    <span class="sr-only" role="status" aria-live="polite" data-ctx-live></span>
    <div class="ctx-panel" id="ctx-panel" role="dialog" aria-labelledby="ctx-title" tabindex="-1" hidden>
      <h2 class="ctx-title" id="ctx-title">Контекст диалога</h2>
      <p class="ctx-text">Бот помнит диалог в пределах контекста: это ваши сообщения, его ответы и результаты команд. Чем длиннее разговор, тем полнее контекст. Когда он заполнится, бот начнёт забывать начало.</p>
      <div class="ctx-meter" data-ctx-info></div>
      <p class="ctx-text t-footnote" id="ctx-compact-note">История останется видимой, бот продолжит по краткой сводке.</p>
      <button type="button" class="btn btn-secondary btn-block" data-ctx-compact aria-describedby="ctx-compact-note">Сжать сейчас</button>
      <p class="t-footnote" data-ctx-busy-hint></p>
      <p class="ctx-status" role="status" aria-live="polite" data-ctx-status></p>
      <button type="button" class="ctx-link" data-ctx-summary-toggle aria-expanded="false" aria-controls="ctx-summary">Показать сводку</button>
      <div class="ctx-summary" id="ctx-summary" role="region" aria-label="Сводка контекста" tabindex="0" data-i18n-skip data-ctx-summary hidden></div>
      <p class="t-footnote" data-ctx-no-summary></p>
    </div>
  </div>`;
}

function ctxInfoHtml(v, thread) {
  let used;
  if (v.tokens === null) {
    used = '<p class="ctx-line">Данных о заполнении пока нет.</p>';
  } else {
    const est = v.estimated ? '<span class="ctx-est">оценка</span>' : '';
    used = v.window !== null
      ? `<p class="ctx-line"><span>Занято ${formatTokens(v.tokens)} из ${formatTokens(v.window)} токенов</span>${est}</p>`
      : `<p class="ctx-line"><span>Занято ${formatTokens(v.tokens)} токенов</span>${est}</p>`;
    if (v.percent !== null) used += `<div class="ctx-bar" aria-hidden="true"><span style="width:${v.percent}%"></span></div>`;
  }
  const last = v.compactedAt
    ? `<p class="ctx-text">Последнее сжатие: ${esc(fmtDateTime(v.compactedAt))}</p>`
    : '<p class="ctx-text">Сжатий пока не было</p>';
  const off = thread.auto_compact_disabled ? '<p class="ctx-text">Автосжатие в этом диалоге отключено</p>' : '';
  return used + last + off;
}

// Контроллер индикатора: хранит занятые ходы (кнопка «Сжать сейчас» заблокирована, пока в треде идёт ход),
// после событий конца хода и после `compacted` перечитывает GET /api/threads/{id}: событие usage контекст не несёт.
function mountContext(root, threadId, thread) {
  if (!root) return null;
  const q = (sel) => root.querySelector(sel);
  const btn = q('[data-ctx-toggle]');
  const panel = q('.ctx-panel');
  const compactBtn = q('[data-ctx-compact]');
  const summaryToggle = q('[data-ctx-summary-toggle]');
  const summaryBox = q('[data-ctx-summary]');
  const state = { thread: thread || {}, busy: new Set(), finished: new Set(), compactTurns: new Set(), requesting: false };
  let open = false;
  let refreshTimer = null;
  let refreshSeq = 0;
  let destroyed = false;

  function paintButton() {
    const v = ctxView(state.thread.context);
    const parts = ctxButtonParts(v);
    root.classList.remove('ctx-ok', 'ctx-warn', 'ctx-crit');
    root.classList.add(`ctx-${ctxLevel(v.percent)}`);
    btn.setAttribute('aria-label', parts.label);
    btn.innerHTML = `${ctxRingHtml(v.percent)}<span class="ctx-pct">${esc(parts.text)}</span>`;
    q('[data-ctx-info]').innerHTML = ctxInfoHtml(v, state.thread);
  }
  function paintCompact() {
    const compacting = state.requesting || Array.from(state.compactTurns).some((id) => state.busy.has(id));
    const busy = compacting || state.busy.size > 0;
    compactBtn.disabled = busy;
    compactBtn.setAttribute('aria-busy', String(compacting));
    compactBtn.textContent = compacting ? 'Сжимаю…' : 'Сжать сейчас';
    q('[data-ctx-busy-hint]').textContent = busy && !compacting ? 'Пока бот отвечает, сжать нельзя.' : '';
  }
  function paintSummary() {
    const text = String(state.thread.summary || '');
    const has = text.trim() !== '';
    summaryToggle.hidden = !has;
    // Пояснение держим в разметке только пока сводки нет: скрытый абзац остался бы в textContent панели.
    const none = q('[data-ctx-no-summary]');
    none.hidden = has;
    none.textContent = has ? '' : 'Сводки пока нет: контекст ещё не сжимали.';
    summaryBox.innerHTML = esc(text);
    if (!has) { summaryBox.hidden = true; summaryToggle.setAttribute('aria-expanded', 'false'); summaryToggle.textContent = 'Показать сводку'; }
  }
  function paintAll() { paintButton(); paintCompact(); paintSummary(); }

  // Статус сжатия: в панели (aria-live polite) и, пока панель закрыта, в скрытой области рядом с кнопкой.
  let progress = false; // в панели сейчас «Сжимаю контекст…»: конец хода без compacted и compact_failed его очищает
  function setStatus(text, error = false, inProgress = false) {
    progress = inProgress;
    const box = q('[data-ctx-status]');
    box.textContent = text;
    box.classList.toggle('is-error', error);
    q('[data-ctx-live]').textContent = open ? '' : text;
  }

  async function refresh() {
    const seq = ++refreshSeq;
    let fresh = null;
    try { fresh = await api.getThread(threadId); } catch { return; }
    if (destroyed || seq !== refreshSeq || !fresh) return;
    state.thread = { ...state.thread, ...fresh };
    paintButton();
    paintSummary();
  }
  function scheduleRefresh() {
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(refresh, 150);
  }

  function openPanel() {
    open = true;
    panel.hidden = false;
    btn.setAttribute('aria-expanded', 'true');
    q('[data-ctx-live]').textContent = '';
    panel.focus();
  }
  function closePanel(returnFocus) {
    if (!open) return;
    open = false;
    panel.hidden = true;
    btn.setAttribute('aria-expanded', 'false');
    if (returnFocus) btn.focus();
  }

  btn.addEventListener('click', () => { if (open) closePanel(true); else openPanel(); });
  summaryToggle.addEventListener('click', () => {
    const show = summaryBox.hidden;
    summaryBox.hidden = !show;
    summaryToggle.setAttribute('aria-expanded', String(show));
    summaryToggle.textContent = show ? 'Скрыть сводку' : 'Показать сводку';
  });
  compactBtn.addEventListener('click', async () => {
    if (compactBtn.disabled) return;
    state.requesting = true;
    setStatus('Сжимаю контекст…', false, true);
    panel.focus(); // кнопка сейчас станет недоступной: фокус остаётся в панели
    paintCompact();
    try {
      const turn = await api.compactThread(threadId);
      state.requesting = false;
      if (turn && turn.id != null) {
        const id = String(turn.id);
        state.compactTurns.add(id);
        if (!state.finished.has(id)) state.busy.add(id);
      }
    } catch (err) {
      state.requesting = false;
      if (err && err.status === 409) setStatus('Сейчас сжать нельзя: бот занят или в диалоге пока нечего сжимать.', true);
      else if (err && err.status) setStatus('Не удалось запустить сжатие. Попробуйте ещё раз.', true);
      else setStatus('Сервер не отвечает. Проверьте сеть или VPN.', true);
    }
    paintCompact();
  });
  const onKey = (e) => { if (open && e.key === 'Escape') { e.preventDefault(); closePanel(true); } };
  const onDocClick = (e) => {
    if (!open || root.contains(e.target)) return;
    closePanel(false);
    // Клик по тексту или пустому месту возвращает фокус на кнопку (на Mac клик уже увёл его на main[tabindex=-1]);
    // клик по ссылке, полю или кнопке оставляет фокус на них.
    const target = e.target instanceof Element ? e.target : null;
    if (!target || !target.closest(CTX_TAB_STOP)) btn.focus();
  };
  document.addEventListener('keydown', onKey);
  document.addEventListener('click', onDocClick);
  // Уход фокуса на другой элемент управления (Tab, клик по ссылке) закрывает панель без возврата фокуса. Контейнеры с
  // tabindex=-1 (main на Mac) не в счёт: клик по ним закрывает панель в onDocClick и возвращает фокус на кнопку.
  root.addEventListener('focusout', (e) => {
    const next = e.relatedTarget;
    if (open && next && !root.contains(next) && next.matches(CTX_TAB_STOP)) closePanel(false);
  });

  paintAll();

  const ctl = {
    isCompactTurn: (id) => state.compactTurns.has(String(id)),
    // События, пришедшие между чтением треда и чтением ленты, разбираются как история (live=false) и refresh не вызывают.
    syncAfterReplay: scheduleRefresh,
    // Ход, который владелец только что отправил: до события status running кнопка уже заблокирована.
    turnStarted(id) {
      if (destroyed || id == null || state.finished.has(String(id))) return;
      state.busy.add(String(id));
      paintCompact();
    },
    onEvent(ev, live) {
      if (destroyed) return;
      const payload = ev.payload || {};
      if (ev.kind === 'status') {
        const raw = payload.turn_id != null && payload.turn_id !== '' ? payload.turn_id : ev.turn_id;
        if (raw == null || raw === '') return;
        const id = String(raw);
        if (payload.status === 'running' || payload.status === 'queued') {
          if (!state.finished.has(id)) state.busy.add(id);
        } else if (CTX_DONE.includes(payload.status)) {
          state.busy.delete(id);
          state.finished.add(id);
          if (live) scheduleRefresh();
          if (live && progress && state.compactTurns.has(id)) setStatus('');
        } else {
          return;
        }
        paintCompact();
        return;
      }
      if (ev.kind !== 'compacted' && ev.kind !== 'compact_failed' && ev.kind !== 'auto_compact_disabled') return;
      if (ev.turn_id != null && ev.turn_id !== '') state.compactTurns.add(String(ev.turn_id));
      if (!live) return;
      if (ev.kind === 'compacted') {
        setStatus(payload.auto === true ? 'Контекст сжат автоматически' : 'Контекст сжат');
        scheduleRefresh();
      } else if (ev.kind === 'compact_failed') {
        setStatus('Не удалось сжать контекст', true);
      } else {
        state.thread = { ...state.thread, auto_compact_disabled: true };
        paintButton();
      }
    },
    destroy() {
      destroyed = true;
      clearTimeout(refreshTimer);
      document.removeEventListener('keydown', onKey);
      document.removeEventListener('click', onDocClick);
    },
  };
  return ctl;
}

// Подключает ленту треда и индикатор контекста; возвращает функцию остановки для setCleanup.
async function startThreadStream(threadId, thread, body, botAvatar) {
  const ctl = mountContext(app.querySelector('[data-ctx]'), threadId, thread);
  const stop = await connectThreadStream(threadId, body, botAvatar, ctl);
  if (ctl) ctl.syncAfterReplay();
  activeCtx = ctl;
  return () => {
    stop();
    if (ctl) ctl.destroy();
    if (activeCtx === ctl) activeCtx = null;
  };
}

async function viewThread(threadId) {
  const thread = await api.getThread(threadId);
  if (!thread) { app.innerHTML = `<div class="center-screen"><p class="t-body">Тред не найден.</p><a class="btn btn-secondary" href="#/">На главную</a></div>`; return; }
  const bots = await listBotsView();
  const bot = bots.find((b) => b.id === thread.bot_id) || {};
  const avatar = avatarHtml(bot.avatar || 'robot', bot.provider, 36);
  const isIncident = thread.kind === 'incident';

  app.innerHTML = `<div class="screen">
    ${backHeader({
      title: (isIncident ? `Инцидент · ${bot.name}` : bot.name) || 'Тред',
      subtitle: [botModelLine(bot), bot.location].filter(Boolean).join(' · '),
      backHref: '#/', avatar,
      right: `${ctxHtml()}${bot.id ? `<a href="#/bots/${esc(bot.id)}?from=${encodeURIComponent(`/threads/${threadId}`)}" aria-label="Настройки бота" data-icon="sliders" class="icon-btn sunken">${ICONS.sliders}</a>` : ''}${hasBrowser(bot) ? `<a href="#/bots/${esc(bot.id)}/browser" aria-label="Экран бота" class="icon-btn sunken">${ICONS.screen}</a>` : ''}`,
    })}
    <div class="thread-state">${botStateBanner(bot)}</div>
    <div id="thread-body" class="thread-body"></div>
    <div class="composer">
      <div class="composer-row1">
        <button type="button" class="model-chip">${esc(modelTitle(bot.model))}${ICONS.chevronDown}</button>
        <label class="t-footnote" style="display:flex;align-items:center;gap:6px;"><input type="checkbox" style="width:18px;height:18px;margin:0;" ${thread.dry_run ? 'checked' : ''}>Пробный прогон</label>
      </div>
      <div class="composer-row2">
        <label class="sr-only" for="composer-input">Сообщение</label>
        <input id="composer-input" class="composer-input" type="text" placeholder="Сообщение" data-thread="${threadId}" disabled>
        <button type="button" aria-label="Голосовой ввод" class="icon-btn round">${ICONS.mic}</button>
        <button type="button" data-action="send-message" data-thread="${threadId}" aria-label="Отправить" disabled class="icon-btn round" style="background:var(--bg-emphasis);color:var(--fg-on-emphasis);">${ICONS.send}</button>
      </div>
      <div data-bot-hint-slot>${botStartHint(bot)}</div>
    </div>
  </div>`;

  const body = document.getElementById('thread-body');
  const stop = await startThreadStream(threadId, thread, body, bot.avatar);
  // Ввод открывается только после подключения к потоку треда: иначе событие отправленного сообщения приходит до подписки и теряется.
  app.querySelectorAll('#composer-input, [data-action="send-message"]').forEach((el) => { el.disabled = false; });
  setCleanup(stop);
}

// ---------------------------------------------------------------------------
// Экран браузера бота (телефон): экран сверху, статус и шаги ниже, главное действие внизу
// ---------------------------------------------------------------------------
async function viewBrowser(botId) {
  const [bots, thread] = await Promise.all([listBotsView(), api.getThreadByBot(botId).catch(() => null)]);
  const bot = bots.find((b) => b.id === botId);
  app.innerHTML = `<div class="screen br-screen">
    ${backHeader({
      title: `Браузер · ${bot ? bot.name : 'бот'}`, subtitle: 'Свой браузер бота',
      backHref: thread ? `#/threads/${thread.id}` : '#/',
      avatar: bot ? avatarHtml(bot.avatar || 'robot', bot.provider, 36) : '',
    })}
    ${browserBodyHtml({ bot, desktop: false })}
    <div class="action-bar br-bar" hidden><div class="br-actions" id="br-actions"></div></div>
  </div>`;
  mountBrowser({ app, bot, botId, setCleanup });
}

// ---------------------------------------------------------------------------
// Approvals (лист-подтверждение)
// ---------------------------------------------------------------------------
async function viewApprovals(focusId) {
  const [rawPending, bots] = await Promise.all([api.listApprovals('pending'), listBotsView()]);
  const pending = focusId ? [...rawPending].sort((a, b) => (a.id === focusId ? -1 : b.id === focusId ? 1 : 0)) : rawPending;
  if (!pending.length) {
    app.innerHTML = `<div class="screen">
      <div class="root-header"><h1 class="h-large-title">Решения</h1></div>
      <div class="center-screen" style="flex-grow:1;">
        <span style="color:var(--success-fg);">${icon('<path d="M5 12l5 5 9-10"></path>', 32, 2.4)}</span>
        <p class="t-body">Нет ожидающих решений.</p>
      </div>
      ${tabBar('approvals', 0)}
    </div>`;
    return;
  }
  const a = pending[0];
  const bot = bots.find((b) => b.id === a.bot_id) || {};
  const avatar = avatarHtml(bot.avatar || 'robot', bot.provider, 36);
  const expiresMin = Math.max(1, Math.round((new Date(a.expires_at).getTime() - Date.now()) / 60000));
  const args = Object.entries(a.args || {}).map(([k, v]) => `${k}: ${v}`).join('\n');
  app.innerHTML = `<div class="screen">
    <div class="sheet-backdrop"></div>
    <section aria-labelledby="ap-title" class="sheet">
      <div class="sheet-grabber"></div>
      <div class="row gap-3">
        ${avatar}
        <div class="flex-1 stack">
          <h2 id="ap-title" class="h-title">Подтверждение</h2>
          <span class="t-footnote">${esc(bot.name)} · 1 из ${pending.length} · истекает через ${expiresMin} мин</span>
        </div>
      </div>
      <div class="risk-card">
        <div class="risk-tag">${ICONS.alert}${esc(RISK_LABEL[a.risk] || 'ДЕЙСТВИЕ')}</div>
        <div class="t-callout" style="font-size:17px;font-weight:500;">${esc(a.title)}</div>
        <dl class="kv" data-i18n-skip>${Object.entries(a.args || {}).map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('')}</dl>
        <details><summary class="t-footnote" style="color:var(--attention-text);cursor:pointer;min-height:32px;display:flex;align-items:center;">Аргументы действия</summary>
          <pre class="args-pre">${esc(a.tool)}\n${esc(args)} · sha ${esc((a.args_hash || '').slice(0, 8))}…</pre>
        </details>
      </div>
      <label class="row gap-2" style="align-items:flex-start;">
        <input type="checkbox" data-remember="${a.id}" style="width:20px;height:20px;margin:0;flex-shrink:0;">
        <span class="t-body">Разрешать ${esc(bot.name)} такие действия без вопроса<br><span class="t-footnote">Правило можно отключить в настройках бота</span></span>
      </label>
      <div class="btn-row btn-row-3">
        <button type="button" class="btn btn-secondary" data-action="reject" data-id="${a.id}">Отклонить</button>
        <button type="button" class="btn btn-secondary" data-nav="#/threads/${esc(a.thread_id)}">Изменить</button>
        <button type="button" class="btn btn-attention" data-action="approve" data-id="${a.id}">Разрешить</button>
      </div>
    </section>
  </div>`;
}

// ---------------------------------------------------------------------------
// Routines
// ---------------------------------------------------------------------------
function scheduleIconFor(kind) {
  if (kind === 'cron') return ICONS.routines;
  if (kind === 'hook') return ICONS.lightning;
  if (kind === 'mac_folder') return ICONS.folder;
  return ICONS.routines;
}
async function viewRoutines() {
  const [schedules, bots, procedures] = await Promise.all([api.listSchedules(), listBotsView(), api.listProcedures().catch(() => null)]);
  const byBot = (id) => bots.find((b) => b.id === id)?.name || id;
  const cron = schedules.filter((s) => s.kind === 'cron');
  const hooks = schedules.filter((s) => s.kind !== 'cron');
  const row = (s) => `<div class="list-row">
    <span class="row-icon">${scheduleIconFor(s.kind)}</span>
    <a href="#/routines/${esc(s.id)}" class="row-body">
      <span class="row-title" data-i18n-skip>${esc(s.name)}</span>
      <span class="row-sub">${esc(byBot(s.bot_id))} · ${esc(s.kind === 'cron' ? (s.cron || '') : (s.kind === 'hook' ? 'webhook' : 'Mac · новый файл'))}</span>
      <span class="row-meta">${s.enabled === false ? `на паузе${s.paused_since ? ' с ' + esc(s.paused_since) : ''}` : (s.last_run ? `${s.last_run.at ? esc(s.last_run.at) : ''}${s.last_run.detail ? ' · ' + esc(s.last_run.detail) : ''}` : '')}</span>
      ${skipNoteHtml(s)}
    </a>
    ${s.enabled === false
      ? `<button type="button" class="btn btn-ghost" data-action="toggle-schedule" data-id="${s.id}">Возобновить</button>`
      : `<button type="button" aria-label="Запустить сейчас" class="icon-btn sunken" data-action="run-schedule" data-id="${s.id}">${ICONS.play}</button>`}
  </div>`;
  app.innerHTML = `<div class="screen">
    <div class="root-header"><h1 class="h-large-title">Рутины</h1><button type="button" class="btn btn-secondary">${ICONS.plus}Новая</button></div>
    <div class="bot-list">
      ${procedureEntryRow(procedures)}
      ${cron.length ? `<h2 class="section-label">По расписанию</h2>${cron.map(row).join('')}` : ''}
      ${hooks.length ? `<h2 class="section-label">По событию</h2>${hooks.map(row).join('')}` : ''}
    </div>
    ${tabBar('routines', (await api.listApprovals('pending')).length)}
  </div>`;
}

// Вход в раздел процедур: сводка по списку, сами процедуры на экране #/procedures.
function procedureEntryRow(procedures) {
  const live = procedures ? procedures.filter((p) => p.status !== 'archived') : [];
  const drafts = live.filter((p) => p.status === 'draft').length;
  const sub = !procedures ? 'Записанные действия ботов в браузере'
    : !live.length ? 'Пока нет: запишите из действий бота или импортируйте'
      : `${live.length} ${live.length === 1 ? 'процедура' : live.length < 5 ? 'процедуры' : 'процедур'}${drafts ? `, черновиков ${drafts}` : ''}`;
  return `<a class="list-row" href="#/procedures" data-nav-procedures>
    <span class="row-icon" aria-hidden="true">${ICONS.checklist}</span>
    <span class="row-body"><span class="row-title">Процедуры</span><span class="row-sub">${esc(sub)}</span></span>
    <span class="row-chevron" aria-hidden="true">${ICONS.chevronRight}</span>
  </a>`;
}

// ---------------------------------------------------------------------------
// Рутина (расписание, событие): параметры и запуск
// ---------------------------------------------------------------------------
async function viewSchedule(id) {
  const [schedules, bots] = await Promise.all([api.listSchedules(), listBotsView()]);
  const s = schedules.find((x) => x.id === id);
  if (!s) { app.innerHTML = `<div class="center-screen"><p class="t-body">Не найдено.</p><a class="btn btn-secondary" href="#/routines">К рутинам</a></div>`; return; }
  const bot = bots.find((b) => b.id === s.bot_id) || {};
  app.innerHTML = `<div class="screen">
    ${backHeader({ title: s.name, subtitle: `Рутина · ${esc(bot.name || '')}`, backHref: '#/routines', avatar: avatarHtml(bot.avatar || 'robot', bot.provider, 36) })}
    <div class="thread-body">
      <div class="card card-pad stack gap-2">
        <span class="t-headline">Параметры</span>
        <div class="t-body" data-i18n-skip>${esc(s.kind === 'cron' ? (s.cron || '') : s.kind)}</div>
        <div class="t-footnote" data-i18n-skip>${esc(s.prompt || '')}</div>
        ${skipNoteHtml(s)}
        ${s.kind === 'cron' ? `<div class="switch-row"><label for="sch-catch-up">Один запуск после возобновления<span class="t-footnote">Если расписание стояло из-за недоступного компьютера бота, после возвращения выполнится один запуск. Пропущенные пачкой не догоняются.</span></label><input id="sch-catch-up" type="checkbox" role="switch" data-action="toggle-catch-up" data-id="${esc(s.id)}"${s.catch_up ? ' checked' : ''}></div>` : ''}
      </div>
    </div>
    <div style="padding:10px 16px 28px;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;background:var(--bg-surface);border-top:1px solid var(--border-default);">
      <button type="button" class="btn btn-secondary" data-action="toggle-schedule" data-id="${s.id}">${s.enabled === false ? 'Возобновить' : 'Поставить на паузу'}</button>
      <button type="button" class="btn btn-primary" data-action="run-schedule" data-id="${s.id}">${ICONS.play}Запустить</button>
    </div>
  </div>`;
}

// ---------------------------------------------------------------------------
// Usage
// ---------------------------------------------------------------------------
let currentUsageDays = 7;

function formatTokens(num) {
  if (num === null || num === undefined) return '0';
  return String(num).replace(/\B(?=(\d{3})+(?!\d))/g, '\u202F');
}

function formatTokensShort(num) {
  if (num === null || num === undefined || num === 0) return '0';
  if (num >= 1000000) {
    const val = (num / 1000000).toFixed(1).replace('.0', '').replace('.', ',');
    return `${val}\u202Fмлн`;
  }
  if (num >= 1000) {
    const val = (num / 1000).toFixed(1).replace('.0', '').replace('.', ',');
    return `${val}\u202Fтыс.`;
  }
  return formatTokens(num);
}

const MONTH_NAMES_SHORT = ['янв', 'фев', 'мар', 'апр', 'май', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек'];
const MONTH_NAMES_GENITIVE = ['января', 'февраля', 'марта', 'апреля', 'мая', 'июня', 'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря'];

function formatUsageDateFull(dateStr) {
  if (!dateStr) return '';
  const parts = dateStr.split('-');
  if (parts.length !== 3) return dateStr;
  const day = parseInt(parts[2], 10);
  const monthIdx = parseInt(parts[1], 10) - 1;
  const year = parts[0];
  const month = MONTH_NAMES_GENITIVE[monthIdx] || '';
  return `${day} ${month} ${year}`;
}

function formatUsageDateShort(dateStr, days) {
  if (!dateStr) return '';
  const parts = dateStr.split('-');
  if (parts.length !== 3) return dateStr;
  const day = parseInt(parts[2], 10);
  const monthIdx = parseInt(parts[1], 10) - 1;
  const month = MONTH_NAMES_SHORT[monthIdx] || '';
  if (days === 1) return 'Сегодня';
  if (days <= 7) return `${day} ${month}`;
  return `${day}`;
}

function meterRow(label, pct, colorVar) {
  const known = typeof pct === 'number';
  return `<div class="meter-row">
    <span class="meter-label">${esc(label)}</span>
    <div role="meter" aria-label="${esc(label)}: ${known ? pct + '%' : 'нет данных'}" ${known ? `aria-valuenow="${pct}"` : ''} aria-valuemin="0" aria-valuemax="100" class="meter-track">
      <div class="meter-fill" style="width:${known ? pct : 0}%;background:${colorVar};"></div>
    </div>
    <span class="meter-pct" style="${known && pct >= 100 ? 'color:var(--danger-fg);' : ''}">${known ? pct + '%' : '-'}</span>
  </div>`;
}

function usageBodyHtml(summary, bots, days) {
  const summaryBots = summary.bots || [];
  const models = summary.models || [];
  const daily = summary.daily || [];
  const total = summary.total_tokens || 0;
  const guard = summary.guard;
  const guardBot = guard && bots.find((b) => b.id === guard.bot_id);
  const providerColor = { claude: 'var(--claude-fg)', codex: 'var(--codex-fg)', gemini: 'var(--gemini-fg)' };

  const periodSwitcher = `
    <div class="usage-period-segmented" role="radiogroup" aria-label="Период">
      <button type="button" class="usage-period-btn ${days === 1 ? 'active' : ''}" data-action="set-usage-days" data-days="1" role="radio" aria-checked="${days === 1 ? 'true' : 'false'}" aria-label="Период: Сегодня">Сегодня</button>
      <button type="button" class="usage-period-btn ${days === 7 ? 'active' : ''}" data-action="set-usage-days" data-days="7" role="radio" aria-checked="${days === 7 ? 'true' : 'false'}" aria-label="Период: 7 дней">7 дней</button>
      <button type="button" class="usage-period-btn ${days === 30 ? 'active' : ''}" data-action="set-usage-days" data-days="30" role="radio" aria-checked="${days === 30 ? 'true' : 'false'}" aria-label="Период: 30 дней">30 дней</button>
    </div>
  `;

  const periodLabels = { 1: 'сегодня', 7: 'за 7 дней', 30: 'за 30 дней' };
  const periodLabel = periodLabels[days] || `за ${days} дн.`;
  const maxTokens = Math.max(...daily.map((d) => d.total_tokens || 0), 1);

  const chartCard = `
    <div class="card usage-chart-card">
      <div class="usage-chart-header">
        <div class="stack gap-1">
          <span class="t-footnote">Всего за период (${esc(periodLabel)})</span>
          <span class="usage-chart-total">${formatTokens(total)} <span class="t-footnote" style="font-weight:400;">токенов</span></span>
        </div>
        <span class="badge badge-sunken">${formatTokensShort(total)}</span>
      </div>
      <div class="usage-chart-wrap" role="region" aria-label="График расхода по дням">
        <div class="usage-chart" role="img" aria-label="График расхода токенов по дням">
          ${daily.map((d) => {
            const tokens = d.total_tokens || 0;
            const pct = tokens > 0 ? Math.max(6, Math.round((tokens / maxTokens) * 100)) : 0;
            const ariaLabel = `${formatUsageDateFull(d.date)}: ${formatTokens(tokens)} токенов`;
            return `
              <div class="usage-bar-col">
                <div class="usage-bar-track">
                  <div class="usage-bar" style="height:${pct}%;" aria-label="${esc(ariaLabel)}" role="img" tabindex="0" title="${esc(ariaLabel)}"></div>
                </div>
                <span class="usage-bar-label">${esc(formatUsageDateShort(d.date, days))}</span>
              </div>
            `;
          }).join('')}
        </div>
        <table class="sr-only">
          <caption>Расход токенов по дням</caption>
          <thead>
            <tr><th scope="col">Дата</th><th scope="col">Токены</th></tr>
          </thead>
          <tbody>
            ${daily.map((d) => `<tr><td>${esc(d.date)}</td><td>${formatTokens(d.total_tokens || 0)}</td></tr>`).join('')}
          </tbody>
        </table>
      </div>
    </div>
  `;

  const botsListCard = `
    <div class="card card-pad stack gap-2">
      <div class="row" style="justify-content:space-between;">
        <span class="t-footnote" style="font-weight:600;">Дневной бюджет ботов</span>
        <span class="t-footnote">сегодня</span>
      </div>
      <div class="stack">
        ${summaryBots.map((u) => {
          const bot = bots.find((b) => b.id === u.bot_id) || { name: u.bot_id, avatar: 'robot', provider: 'claude' };
          const budget = u.budget || 200000;
          const today = u.tokens_today || 0;
          const pct = budget > 0 ? Math.round((today / budget) * 100) : 0;
          const isWarn = pct >= 80 && pct < 100;
          const isDanger = pct >= 100;
          const fillClass = isDanger ? 'meter-fill-danger' : isWarn ? 'meter-fill-warn' : '';
          const color = isDanger ? 'var(--danger-fg)' : isWarn ? 'var(--attention-fg)' : (providerColor[bot.provider] || 'var(--fg-default)');
          const lastAct = u.last_activity ? timeLabel(u.last_activity) : 'нет активности';

          return `
            <div class="usage-bot-row">
              <div class="usage-bot-head">
                ${avatarSlot(bot, 28)}
                <span class="flex-1 stack min-w-0">
                  <span class="t-callout" style="font-size:14px;font-weight:600;" data-i18n-skip>${esc(bot.name || u.bot_id)}</span>
                  <span class="t-footnote" style="font-size:12px;">активность: ${esc(lastAct)}</span>
                </span>
                <span class="t-callout ${isDanger ? 'usage-danger' : isWarn ? 'usage-warn' : ''}" style="font-weight:600;">
                  ${pct}%
                </span>
              </div>
              <div role="meter" aria-label="${esc(bot.name || u.bot_id)}: ${formatTokens(today)} из ${formatTokens(budget)} токенов (${pct}%)"
                   aria-valuenow="${pct}" aria-valuemin="0" aria-valuemax="100" class="meter-track">
                <div class="meter-fill ${fillClass}" style="width:${Math.min(pct, 100)}%;background:${color};"></div>
              </div>
              <div class="usage-bot-meta t-footnote">
                <span>${formatTokens(today)} / ${formatTokens(budget)}</span>
                <span>за период: ${formatTokens(u.tokens_period || today)}</span>
              </div>
            </div>
          `;
        }).join('')}
      </div>
    </div>
  `;

  const modelsCard = `
    <div class="card card-pad stack gap-2">
      <span class="t-footnote" style="font-weight:600;">Расход по моделям</span>
      ${!models.length ? `<p class="t-footnote t-muted" style="margin:8px 0;text-align:center;">За выбранный период вызовов моделей не было.</p>` : `
        <div class="usage-table-wrap">
          <table class="usage-table" aria-label="Таблица расхода токенов по моделям">
            <thead>
              <tr>
                <th scope="col" style="text-align:left;">Модель</th>
                <th scope="col" style="text-align:right;">Вход</th>
                <th scope="col" style="text-align:right;">Выход</th>
                <th scope="col" style="text-align:right;">Кэш чтение</th>
                <th scope="col" style="text-align:right;">Кэш запись</th>
                <th scope="col" style="text-align:right;">Итого</th>
                <th scope="col" style="text-align:right;">Ходы</th>
              </tr>
            </thead>
            <tbody>
              ${models.map((m) => `
                <tr>
                  <td>
                    <div class="stack gap-1">
                      <span class="t-callout" style="font-size:13px;font-weight:600;" data-i18n-skip>${esc(m.model)}</span>
                      <span class="t-footnote" style="font-size:11px;">${esc(PROVIDER_LABEL[m.provider] || m.provider)}</span>
                    </div>
                  </td>
                  <td style="text-align:right;" class="t-log">${formatTokens(m.tokens_in)}</td>
                  <td style="text-align:right;" class="t-log">${formatTokens(m.tokens_out)}</td>
                  <td style="text-align:right;" class="t-log">${formatTokens(m.tokens_cache_read)}</td>
                  <td style="text-align:right;" class="t-log">${formatTokens(m.tokens_cache_write)}</td>
                  <td style="text-align:right;font-weight:600;" class="t-log">${formatTokens(m.total_tokens)}</td>
                  <td style="text-align:right;" class="t-log">${formatTokens(m.turns)}</td>
                </tr>
              `).join('')}
            </tbody>
          </table>
        </div>
      `}
    </div>
  `;

  const guardCard = guard ? `
    <div class="card card-pad stack gap-2" style="background:var(--danger-bg);color:var(--danger-fg);border:none;">
      <div class="row gap-2" style="align-items:flex-start;font:600 15px/20px var(--font-sans);">
        ${ICONS.stop}<span>${esc(guardBot?.name || guard.bot_id)} остановлен предохранителем: ${esc(guard.reason === 'repeat_error' ? '3 раза одна ошибка' : guard.reason)}</span>
      </div>
      <code class="t-log" style="color:var(--fg-default);">${esc(guard.detail)}</code>
      <div class="btn-row btn-row-2">
        <a href="#/threads/${esc(guard.thread_id)}" class="btn btn-secondary">Открыть тред</a>
        <a href="#/bots/${esc(guard.bot_id)}" class="btn btn-secondary">Другая модель</a>
      </div>
    </div>
  ` : '';

  return `
    ${periodSwitcher}
    ${chartCard}
    ${guardCard}
    ${botsListCard}
    ${modelsCard}
  `;
}

// Контент экрана расхода для обеих раскладок: данные, пустое состояние или ошибка с повтором.
// Короткая подпись давности для экрана расхода (у редактора памяти своя копия в memory.js).
function timeLabel(iso) {
  const d = new Date(iso);
  const days = Math.floor((Date.now() - d.getTime()) / 86400000);
  if (days <= 0) return 'сегодня';
  if (days === 1) return 'вчера';
  return d.toLocaleDateString(locale(), { day: 'numeric', month: 'short' });
}

async function usageContentHtml() {
  try {
    const [summary, bots] = await Promise.all([api.usageSummary(currentUsageDays), listBotsView()]);
    if ((!summary.bots || !summary.bots.length) && (!summary.daily || !summary.daily.length) && (!summary.models || !summary.models.length)) {
      return `
        <div class="state-box">
          <div class="state-icon">${ICONS.usage}</div>
          <h2 class="state-title">Расхода пока нет</h2>
          <p class="state-text t-footnote">Когда боты начнут выполнять задачи, здесь появится статистика использования токенов и дневного бюджета.</p>
        </div>
      `;
    }
    return usageBodyHtml(summary, bots, currentUsageDays);
  } catch (err) {
    return `
      <div class="state-box state-error" role="alert">
        <div class="state-icon">${ICONS.alert}</div>
        <h2 class="state-title">Не получилось загрузить расход</h2>
        <p class="state-text t-footnote">${esc(err && (err.detail || err.message || err))}</p>
        <div class="state-actions">
          <button type="button" class="btn btn-secondary" data-action="retry-usage">Повторить</button>
        </div>
      </div>
    `;
  }
}

async function viewUsage() {
  const approvals = await api.listApprovals('pending').catch(() => []);
  app.innerHTML = `<div class="screen">
    <div class="root-header"><h1 class="h-large-title">Расход</h1></div>
    <div class="bot-list" id="usage-content">
      <div class="state-box" role="status" aria-label="Загружаю расход">
        <div class="spin" style="font-size:24px;">${ICONS.spinner}</div>
        <p class="state-text t-footnote">Загружаю расход…</p>
      </div>
    </div>
    ${tabBar('usage', approvals.length)}
  </div>`;

  const content = document.getElementById('usage-content');
  if (content) content.innerHTML = await usageContentHtml();
}

// ---------------------------------------------------------------------------
// Новый бот: описание → черновик (docs/contracts.md §9, POST /api/bots/draft)
// ---------------------------------------------------------------------------
function loadingBtnHtml(text) {
  return `<span class="spin">${ICONS.spinner}</span>${esc(text)}`;
}

// Черновик описания бота: уходя подключать модель, человек оставляет текст в sessionStorage, при возврате он на месте.
// Чтение не стирает черновик: экран может отрисоваться дважды подряд (возврат по истории), и вторая отрисовка
// получала пустое поле. Черновик стирается при создании бота и когда человек сам очистил поле (draftSet('')).
const NEW_BOT_DRAFT_KEY = 'bothub_new_bot_draft';
function draftTake() {
  try { return sessionStorage.getItem(NEW_BOT_DRAFT_KEY) || ''; } catch { return ''; }
}
function draftSet(text) {
  try {
    if (text) sessionStorage.setItem(NEW_BOT_DRAFT_KEY, text);
    else { sessionStorage.removeItem(NEW_BOT_DRAFT_KEY); sessionStorage.removeItem(NEW_BOT_AVATAR_KEY); }
  } catch { /* приватный режим: черновик не сохранится */ }
}
// Персонаж нового бота живёт вместе с черновиком: возврат на шаг 1 и повторная сборка, как и уход к провайдерам, его не меняют.
// Без текста черновика сохранённый персонаж не берётся: новый мастер начинается со случайного.
const NEW_BOT_AVATAR_KEY = 'bothub_new_bot_avatar';
function avatarDraftTake() {
  try { const kind = sessionStorage.getItem(NEW_BOT_AVATAR_KEY); return AVATAR_KINDS.includes(kind) ? kind : null; } catch { return null; }
}
function avatarDraftSet(kind) {
  try { sessionStorage.setItem(NEW_BOT_AVATAR_KEY, kind); } catch { /* приватный режим: выбор не сохранится */ }
}

async function viewBotNew(qs) {
  // Печатать текст в textarea через симулятор не всегда получается: в mock-режиме
  // можно подставить описание сразу через #/bots/new?d=... (только ?mock=1).
  const prefill = api.MOCK ? (qs.get('d') || '') : '';
  const restored = draftTake();
  let chosenAvatar = restored ? avatarDraftTake() : null; // персонаж этого мастера; null: ещё не выбран, возьмём случайного

  function renderStep1(initial) {
    app.innerHTML = `<div class="screen">
      ${backHeader({ title: 'Новый бот', backHref: '#/' })}
      <div class="thread-body" style="gap:16px;">
        <div class="card card-pad stack gap-3">
          <div id="bn-models-check" aria-live="polite"></div>
          <div class="field">
            <label for="bn-desc">Что бот должен делать и как работать</label>
            <textarea id="bn-desc" class="bot-new-textarea" rows="6" placeholder="Например: «Каждое утро в 9 смотреть новые вакансии SRE с релокацией в Сербию и присылать 5 лучших с зарплатой»">${esc(initial || '')}</textarea>
          </div>
          <p id="bn-desc-error" class="t-footnote" style="color:var(--danger-fg);" hidden></p>
          <button type="button" id="bn-desc-submit" class="btn btn-primary">Собрать</button>
        </div>
      </div>
    </div>`;
    const descEl = document.getElementById('bn-desc');
    const btn = document.getElementById('bn-desc-submit');
    const errEl = document.getElementById('bn-desc-error');
    const checkEl = document.getElementById('bn-models-check');
    let modelsReady = null; // null: ещё проверяем или не удалось проверить; false: включённых моделей нет
    const sync = () => { btn.disabled = descEl.value.trim().length < 10 || modelsReady === false; };
    descEl.addEventListener('input', sync);
    sync();

    // Модели проверяем до описания: без включённой модели бот не соберётся, и узнавать об этом на втором шаге поздно.
    async function checkModels() {
      checkEl.innerHTML = '<p class="t-footnote"><span class="spin">' + ICONS.spinner + '</span> Проверяю модели</p>';
      try {
        const options = await loadModelOptions();
        modelsReady = usableModels(options.groups).length > 0;
      } catch (err) {
        if (err.status === 401) return;
        modelsReady = null;
        checkEl.innerHTML = `<div class="banner banner-attention" role="status"><span class="banner-icon">${ICONS.alert}</span><span class="banner-text"><span class="banner-title">Не удалось проверить модели</span><span class="banner-sub">Описание можно писать, модель проверится на следующем шаге.</span></span><button type="button" class="btn btn-secondary" data-bn-retry>${ICONS.retry}Повторить</button></div>`;
        checkEl.querySelector('[data-bn-retry]').addEventListener('click', checkModels);
        sync();
        return;
      }
      if (modelsReady) checkEl.innerHTML = '';
      else {
        checkEl.innerHTML = `<div class="banner banner-attention" role="status"><span class="banner-icon">${ICONS.alert}</span><span class="banner-text"><span class="banner-title">Сначала нужна модель</span><span class="banner-sub">Включённых моделей нет, бот не сможет отвечать. Описание сохранится: к нему можно вернуться.</span></span><a class="btn btn-secondary" href="#/settings/providers" data-bn-providers>Подключить модель</a></div>`;
        checkEl.querySelector('[data-bn-providers]').addEventListener('click', () => draftSet(descEl.value));
      }
      sync();
    }
    checkModels();
    btn.addEventListener('click', async () => {
      const text = descEl.value.trim();
      if (text.length < 10) return;
      descEl.disabled = true;
      btn.disabled = true;
      btn.setAttribute('aria-busy', 'true');
      btn.innerHTML = loadingBtnHtml('Собираю…');
      errEl.hidden = true;
      try {
        const draft = await api.createBotDraft(text);
        await renderStep2(draft, text);
      } catch (err) {
        descEl.disabled = false;
        btn.disabled = false;
        btn.removeAttribute('aria-busy');
        btn.textContent = 'Собрать';
        errEl.hidden = false;
        errEl.textContent = err.status === 502 || err.code === 'builder_failed'
          ? 'Не получилось собрать бота. Опишите задачу подробнее'
          : `Не получилось собрать бота: ${err.message || String(err)}`;
      }
    });
  }

  function step2Html(d) {
    return `<div class="screen">
      ${backHeader({ title: 'Новый бот', backHref: '#/' })}
      <div class="thread-body" style="gap:16px;">
        <div class="card card-pad row gap-4" style="justify-content:center;">
          <div id="bn-avatar-big">${avatarHtml(d.avatar, d.provider, 96)}</div>
          <button type="button" id="bn-avatar-random" class="btn btn-secondary" aria-label="Выбрать другого персонажа случайно">${ICONS.retry}Другой</button>
          <span id="bn-avatar-live" class="sr-only" aria-live="polite"></span>
        </div>
        <div class="card card-pad stack gap-3">
          <div class="field"><label for="bn-name">Имя</label><input id="bn-name" type="text" value="${esc(d.name)}"></div>
          <div class="field"><label for="bn-role">Роль</label><input id="bn-role" type="text" value="${esc(d.role)}"></div>
          <div class="stack gap-2">
            <span class="t-footnote">Персонаж</span>
            <div class="avatar-grid">
              ${AVATAR_KINDS.map((k) => `<button type="button" data-bn-avatar="${k}" aria-label="Персонаж: ${esc(avatarLabel(k)).split(':')[0]}" aria-pressed="${k === d.avatar}" class="avatar-pick">${avatarHtml(k, null, 36)}</button>`).join('')}
            </div>
          </div>
        </div>
        <div class="card card-pad stack gap-3">
          <span class="t-headline" style="font-size:15px;">Модель</span>
          <div id="bn-model"></div>
        </div>
        <div class="card card-pad stack gap-3">
          <span class="t-headline" style="font-size:15px;">Где работает</span>
          <div role="radiogroup" class="segmented" style="grid-template-columns:repeat(2,minmax(0,1fr));">
            <button type="button" data-bn-executor="container" role="radio" aria-checked="${d.executor === 'container'}">Сервер</button>
            <button type="button" data-bn-executor="mac" role="radio" aria-checked="${d.executor === 'mac'}">Mac</button>
          </div>
          <div class="switch-row">
            <label for="bn-mfc"><span class="t-body">Полный контроль Mac</span><span class="t-footnote" style="font-size:12px;">Файлы, приложения, клики и ввод, скриншоты</span></label>
            <input id="bn-mfc" type="checkbox" role="switch" ${d.mac_full_control ? 'checked' : ''}>
          </div>
        </div>
        <div class="card card-pad stack gap-3">
          <span class="t-headline" style="font-size:15px;">Можно без вопроса</span>
          <div class="chip-list">
            ${(d.auto_allow || []).length ? d.auto_allow.map((a, i) => `<span class="chip">${esc(toolLabel(a.tool))}<button type="button" data-bn-rule-del="${i}" aria-label="Удалить правило: ${esc(toolLabel(a.tool))}" class="chip-del">${ICONS.close}</button></span>`).join('') : '<span class="t-footnote">Список пуст: бот всё будет спрашивать</span>'}
          </div>
        </div>
        ${d.schedule ? `<div class="card card-pad stack gap-3">
          <div class="switch-row" style="min-height:auto;">
            <label for="bn-sched"><span class="t-headline" style="font-size:15px;">Расписание</span><span class="t-footnote">${esc(humanizeCron(d.schedule.cron))}</span></label>
            <input id="bn-sched" type="checkbox" role="switch" ${d.scheduleEnabled ? 'checked' : ''}>
          </div>
          <code class="t-log" style="color:var(--fg-muted);">${esc(d.schedule.cron)}</code>
        </div>` : ''}
        <details class="card card-pad">
          <summary class="t-headline" style="font-size:15px;cursor:pointer;min-height:32px;display:flex;align-items:center;">Инструкции</summary>
          <textarea id="bn-instructions" class="bot-new-textarea" rows="8" style="margin-top:10px;">${esc(d.instructions || '')}</textarea>
        </details>
        <div class="stack gap-1" style="padding:0 4px;">
          <span class="t-footnote" style="font-weight:600;">Почему так</span>
          <span class="t-footnote">${esc(d.rationale || '')}</span>
        </div>
        <p id="bn-create-error" class="t-footnote" style="color:var(--danger-fg);" hidden></p>
        <div class="btn-row btn-row-2">
          <button type="button" id="bn-back" class="btn btn-secondary">Назад</button>
          <button type="button" id="bn-create" class="btn btn-primary"${d.model_id ? '' : ' disabled'}>Создать</button>
        </div>
      </div>
    </div>`;
  }

  async function renderStep2(draft, description) {
    // Модель берётся из включённых моделей своих провайдеров; предложенную черновиком берём, если она есть среди них.
    const options = await loadModelOptions();
    const available = usableModels(options.groups);
    const first = available.find((x) => x.model.name === draft.model) || available[0];
    if (!chosenAvatar) { chosenAvatar = randomAvatar(); avatarDraftSet(chosenAvatar); }
    const d = { ...draft, avatar: chosenAvatar, scheduleEnabled: !!draft.schedule, provider_id: first ? first.provider.id : null, model_id: first ? first.model.id : null };
    if (first) { d.model = first.model.name; d.provider = runnerKind(first.provider); }

    function paint() { app.innerHTML = step2Html(d); wire(); }

    // Смена персонажа без перерисовки экрана: фокус остаётся на нажатой кнопке, введённое не теряется.
    function setAvatar(kind, announce) {
      d.avatar = chosenAvatar = kind;
      avatarDraftSet(kind);
      document.getElementById('bn-avatar-big').innerHTML = avatarHtml(kind, d.provider, 96);
      document.querySelectorAll('[data-bn-avatar]').forEach((b) => b.setAttribute('aria-pressed', String(b.getAttribute('data-bn-avatar') === kind)));
      if (announce) document.getElementById('bn-avatar-live').textContent = `Персонаж: ${avatarLabel(kind).split(':')[0]}`;
    }

    function wire() {
      document.getElementById('bn-name').addEventListener('input', (e) => { d.name = e.target.value; });
      document.getElementById('bn-role').addEventListener('input', (e) => { d.role = e.target.value; });
      document.getElementById('bn-instructions')?.addEventListener('input', (e) => { d.instructions = e.target.value; });
      document.getElementById('bn-mfc')?.addEventListener('change', (e) => { d.mac_full_control = e.target.checked; });
      document.getElementById('bn-sched')?.addEventListener('change', (e) => { d.scheduleEnabled = e.target.checked; });
      document.querySelectorAll('[data-bn-avatar]').forEach((b) => b.addEventListener('click', () => setAvatar(b.getAttribute('data-bn-avatar'), false)));
      document.getElementById('bn-avatar-random').addEventListener('click', () => setAvatar(randomAvatar(d.avatar), true));
      renderModelPicker(document.getElementById('bn-model'), {
        groups: options.groups,
        selected: () => d.model_id,
        sheetSubtitle: 'Для нового бота',
        onPick: async (provider, model) => {
          d.provider_id = provider.id; d.model_id = model.id; d.model = model.name; d.provider = runnerKind(provider);
          document.getElementById('bn-avatar-big').innerHTML = avatarHtml(d.avatar, d.provider, 96);
          document.getElementById('bn-create').disabled = false;
        },
      });
      document.querySelectorAll('[data-bn-executor]').forEach((b) => b.addEventListener('click', () => {
        d.executor = b.getAttribute('data-bn-executor');
        if (d.executor !== 'mac') d.mac_full_control = false;
        paint();
      }));
      document.querySelectorAll('[data-bn-rule-del]').forEach((b) => b.addEventListener('click', () => {
        d.auto_allow.splice(Number(b.getAttribute('data-bn-rule-del')), 1);
        paint();
      }));
      document.getElementById('bn-back').addEventListener('click', () => renderStep1(description));
      document.getElementById('bn-create').addEventListener('click', async () => {
        const btn = document.getElementById('bn-create');
        const errEl = document.getElementById('bn-create-error');
        btn.disabled = true;
        btn.setAttribute('aria-busy', 'true');
        btn.innerHTML = loadingBtnHtml('Создаю…');
        errEl.hidden = true;
        try {
          const payload = {
            id: d.id, name: d.name, role: d.role, avatar: d.avatar, provider: d.provider, model: d.model,
            executor: d.executor, mac_full_control: d.mac_full_control, auto_allow: d.auto_allow,
            instructions: d.instructions, start_container: true,
          };
          if (d.schedule && d.scheduleEnabled) payload.schedule = d.schedule;
          payload.provider_id = d.provider_id;
          payload.model_id = d.model_id;
          const bot = await api.createBot(payload);
          if (bot.container && String(bot.container).startsWith('failed:')) {
            alert(`Бот создан, компьютер бота не запустился: ${String(bot.container).slice(bot.container.indexOf(':') + 1).trim()}`);
          }
          draftSet('');
          const thread = await api.createThread(bot.id, '');
          location.hash = `#/threads/${thread.id}`;
        } catch (err) {
          btn.disabled = false;
          btn.removeAttribute('aria-busy');
          errEl.hidden = false;
          if (api.isLostResponse(err)) {
            // Ответ пропал, а бота в списке нет (createBot уже перечитал список): повторить можно, дубля не будет.
            btn.textContent = 'Повторить';
            errEl.textContent = 'Сервер не ответил, бот не создан. Проверьте сеть и повторите.';
          } else {
            btn.textContent = 'Создать';
            errEl.textContent = `Не получилось создать бота: ${err.message || String(err)}`;
          }
        }
      });
    }

    paint();
  }

  renderStep1(prefill || restored);
}

// ---------------------------------------------------------------------------
// BotSettings
// ---------------------------------------------------------------------------
// Карточки настроек бота: одни и те же на телефоне и на Mac. Родитель с data-bot нужен делегированию в handleAction.
function botSettingsCards(bot) {
  botState.set(bot.id, bot);
  const acOn = bot.auto_compact_percent !== null; // нет поля (старое ядро) считаем включённым по умолчанию 80
  const acPercent = Number.isInteger(bot.auto_compact_percent) ? bot.auto_compact_percent : 80;
  return `<div class="form-alert" id="bot-alert"></div>
      <div data-bot-state-slot class="stack gap-2">${botStateBanner(bot)}</div>
      <div class="card card-pad stack gap-3">
        <div class="row gap-4">
          ${avatarSlot(bot, 64)}
          <div class="flex-1 stack gap-1">
            <span class="t-headline" data-i18n-skip>${esc(bot.name)}</span>
            <span class="t-footnote" data-i18n-skip>${esc(bot.role || '')}</span>
          </div>
        </div>
        <div class="avatar-grid">
          ${AVATAR_KINDS.map((k) => `<button type="button" data-action="pick-avatar" data-avatar="${k}" aria-label="Персонаж: ${esc(avatarLabel(k)).split(':')[0]}" aria-pressed="${k === bot.avatar}" class="avatar-pick">${avatarHtml(k, null, 36)}</button>`).join('')}
        </div>
      </div>
      ${botModelCardHtml(bot)}
      <div class="card card-pad stack gap-3">
        <span class="t-headline" style="font-size:15px;">Где работает</span>
        <div role="radiogroup" class="segmented" style="grid-template-columns:repeat(2,minmax(0,1fr));">
          <button type="button" data-action="set-executor" data-value="container" role="radio" aria-checked="${bot.executor === 'container'}">Сервер</button>
          <button type="button" data-action="set-executor" data-value="mac" role="radio" aria-checked="${bot.executor === 'mac'}">Mac</button>
        </div>
        <div class="switch-row">
          <label for="mfc"><span class="t-body">Полный контроль Mac</span><span class="t-footnote" style="font-size:12px;">Файлы, приложения, клики и ввод, скриншоты</span></label>
          <input id="mfc" data-action="toggle-mfc" type="checkbox" role="switch" ${bot.mac_full_control ? 'checked' : ''}>
        </div>
      </div>
      <div class="card card-pad stack gap-2 ac-card">
        <div class="switch-row">
          <label for="ac-switch"><span class="t-body">Автосжатие контекста</span><span class="t-footnote" style="font-size:12px;">Когда диалог заполнится до порога, бот сам сожмёт его в краткую сводку</span></label>
          <input id="ac-switch" data-action="toggle-autocompact" type="checkbox" role="switch" ${acOn ? 'checked' : ''}>
        </div>
        <div class="ac-slider-row">
          <label class="t-footnote" for="ac-range">Порог</label>
          <input id="ac-range" data-action="set-autocompact-percent" type="range" min="50" max="95" step="5" value="${acPercent}" ${acOn ? '' : 'disabled'}>
          <output class="ac-value" for="ac-range" data-ac-value>${acPercent}%</output>
        </div>
      </div>
      <div class="card card-pad stack gap-2">
        <span class="t-headline" style="font-size:15px;">Правила</span>
        <span class="t-footnote">Без вопроса: чтение файлов, поиск, скриншот</span>
        <span class="t-footnote" style="color:var(--attention-text);">Всегда спрашивать: удаление, отправка, оплата, вход</span>
        <span class="t-footnote">Предохранитель: ${Math.round((bot.budget_daily_tokens || 200000) / 1000)}k токенов в день, стоп после 3 одинаковых ошибок, до 30 мин на задачу</span>
      </div>
      <div class="card card-pad stack gap-2">
        <span class="t-headline" style="font-size:15px;">Удаление</span>
        <span class="t-footnote">Удаляется только бот без истории: если есть треды, расписания или память, сервер откажет. Файлы бота сохраняются на сервере.</span>
        <button type="button" class="btn btn-danger" data-action="delete-bot" data-bot="${esc(bot.id)}" data-name="${esc(bot.name)}">${ICONS.trash}Удалить бота</button>
      </div>`;
}

// Назад из настроек бота ведёт туда, откуда пришли: в тред (?from=/threads/<id>) или в список.
function settingsBackHref(qs) {
  const from = qs && qs.get('from');
  return from && /^\/threads\/[\w.-]+$/.test(from) ? `#${from}` : '#/';
}

async function viewBotSettings(id, qs) {
  const bots = await listBotsView();
  const bot = bots.find((b) => b.id === id);
  if (!bot) { app.innerHTML = `<div class="center-screen"><p class="t-body">Бот не найден.</p><a class="btn btn-secondary" href="#/">На главную</a></div>`; return; }
  app.innerHTML = `<div class="screen" data-bot="${esc(bot.id)}">
    ${backHeader({ title: bot.name, subtitle: 'Настройки бота', backHref: settingsBackHref(qs), avatar: avatarSlot(bot, 36) })}
    <div class="thread-body" style="gap:10px;">
      ${botSettingsCards(bot)}
    </div>
  </div>`;
  mountBotModel(app.querySelector('.screen[data-bot]'), bot.id);
}

// Выбор модели в настройках бота: подгружает реестр и сохраняет выбор в ядре (provider_id и model_id вместе).
function mountBotModel(scope, botId) {
  const bot = botState.get(botId);
  if (!scope || !bot) return;
  mountBotModelCard(scope, bot, (row) => {
    const before = botState.get(botId) || bot;
    const saved = botView({ ...before, status_label: row.status_label, status_kind: row.status_kind, summary: row.summary, ...row });
    botState.set(botId, saved);
    paintBotSettings(scope, saved);
  });
}

// ---------------------------------------------------------------------------
// Desktop (Mac ≥1024px): три колонки
// ---------------------------------------------------------------------------
async function renderDesktop(route) {
  // usage нужен боковой панели треда (расход по провайдерам); сбой сводки не должен ломать раскладку.
  // На самом экране расхода сводку запрашивает usageContentHtml: второй запрос здесь был бы лишним.
  const [bots, approvals, schedules, usage] = await Promise.all([listBotsView(), api.listApprovals('pending'), api.listSchedules(),
    route.name === 'usage' ? { providers: [], bots: [] } : api.usageSummary().catch(() => ({ providers: [], bots: [] }))]);
  const activeBotId = route.name === 'thread' ? (await api.getThread(route.id))?.bot_id
    : (route.name === 'bot-settings' || route.name === 'browser') ? route.id : (bots[0] && bots[0].id);
  const providerColor = { claude: 'var(--claude-fg)', codex: 'var(--codex-fg)', gemini: 'var(--gemini-fg)' };

  const sidebar = desktopSidebar({ bots, approvals, activeBotId, activeNav: { routines: 'routines', schedule: 'routines', approvals: 'approvals', memory: 'memory', usage: 'usage' }[route.name] || '' });

  let mainHtml, asideHtml = '', threadId = null;
  if (route.name === 'thread') {
    threadId = route.id;
    const thread = await api.getThread(threadId);
    const bot = bots.find((b) => b.id === thread?.bot_id) || {};
    mainHtml = `<main class="desktop-main">
      <div class="desktop-thread-head">${avatarHtml(bot.avatar || 'robot', bot.provider, 36)}
        <div class="flex-1"><div class="t-headline">${esc(bot.name)} ${thread?.kind === 'incident' ? '· Инцидент' : ''}</div><div class="t-footnote">${esc([botModelLine(bot), bot.location].filter(Boolean).join(' · '))}</div></div>
        ${ctxHtml()}${hasBrowser(bot) ? `<a href="#/bots/${esc(bot.id)}/browser" class="btn btn-secondary">${ICONS.screen}Экран</a>` : ''}<a href="#/bots/${esc(bot.id || '')}?from=${encodeURIComponent(`/threads/${threadId}`)}" class="btn btn-secondary">Настройки бота</a><button type="button" class="btn btn-secondary">Новая задача</button>
      </div>
      <div class="thread-state">${botStateBanner(bot)}</div>
      <div id="thread-body" class="desktop-thread-body"></div>
      <div class="desktop-composer">
        <button type="button" class="model-chip">${esc(modelTitle(bot.model))}${ICONS.chevronDown}</button>
        <label class="sr-only" for="composer-input">Сообщение</label>
        <input id="composer-input" class="composer-input" type="text" placeholder="Сообщение ${esc(bot.name || '')}  ⌘↵ отправить" data-thread="${threadId}" disabled>
        <button type="button" data-action="send-message" data-thread="${threadId}" aria-label="Отправить" disabled class="icon-btn round" style="background:var(--bg-emphasis);color:var(--fg-on-emphasis);">${ICONS.send}</button>
      </div>
      <div data-bot-hint-slot>${botStartHint(bot)}</div>
    </main>`;
    asideHtml = `<aside class="desktop-aside">
      <div class="desktop-aside-label">Экран бота</div>
      ${hasBrowser(bot) ? `<div class="card card-pad stack gap-2"><span class="t-callout" data-browser-status role="status">Состояние экрана…</span><a href="#/bots/${esc(bot.id)}/browser" class="btn btn-secondary">${ICONS.screen}Открыть экран</a></div>`
    : `<div class="card card-pad"><span class="t-footnote">У этого бота нет браузера.</span></div>`}
      <div class="desktop-aside-label" style="margin-top:8px;">Квоты</div>
      ${usage.providers.map((p) => meterRow(PROVIDER_LABEL[p.provider] || p.provider, p.pct_week, providerColor[p.provider])).join('')}
    </aside>`;
  } else if (route.name === 'browser') {
    const bot = bots.find((b) => b.id === route.id);
    const thread = await api.getThreadByBot(route.id).catch(() => null);
    mainHtml = `<main class="desktop-main">
      <div class="desktop-thread-head">${bot ? avatarSlot(bot, 36) : ''}
        <div class="flex-1 min-w-0 stack"><h1 class="t-headline header-title">Браузер · ${esc(bot ? bot.name : 'бот')}</h1><span class="t-footnote">Свой браузер бота${thread && thread.title ? ` · тред «${esc(thread.title)}»` : ''}</span></div>
        <div class="br-actions" id="br-actions"></div>
      </div>
      <div class="desktop-thread-body br-desk-body">${browserBodyHtml({ bot, desktop: true })}</div>
    </main>`;
    asideHtml = `<aside class="desktop-aside">${browserAsideHtml()}</aside>`;
  } else if (route.name === 'usage') {
    const usageBody = await usageContentHtml();
    mainHtml = `<main class="desktop-main"><div class="desktop-thread-head"><h1 class="t-headline header-title">Расход</h1></div><div class="desktop-thread-body" style="max-width:640px;gap:12px;">${usageBody}</div></main>`;
  } else if (route.name === 'routines' || route.name === 'schedule') {
    const procedures = await api.listProcedures().catch(() => null);
    mainHtml = `<main class="desktop-main"><div class="desktop-thread-head"><div class="t-headline">Рутины</div></div><div class="desktop-thread-body" style="max-width:640px;">
      ${procedureEntryRow(procedures)}
      ${schedules.map((s) => `<div class="card card-pad row gap-3"><span class="row-icon">${scheduleIconFor(s.kind)}</span><span class="flex-1 stack"><span class="row-title" data-i18n-skip>${esc(s.name)}</span><span class="row-sub">${esc(bots.find((b) => b.id === s.bot_id)?.name || s.bot_id)}</span>${skipNoteHtml(s)}</span><button type="button" class="icon-btn sunken" data-action="run-schedule" data-id="${s.id}">${ICONS.play}</button></div>`).join('')}
    </div></main>`;
  } else if (route.name === 'approvals') {
    const a = (route.id && approvals.find((x) => x.id === route.id)) || approvals[0];
    mainHtml = `<main class="desktop-main"><div class="desktop-thread-head"><div class="t-headline">Решения</div></div><div class="desktop-thread-body" style="max-width:560px;">
      ${a ? `<div class="risk-card">
        <div class="risk-tag">${ICONS.alert}${esc(RISK_LABEL[a.risk] || 'ДЕЙСТВИЕ')}</div>
        <div class="t-callout" style="font-size:17px;font-weight:500;">${esc(a.title)}</div>
        <div class="btn-row btn-row-2"><button type="button" class="btn btn-secondary" data-action="reject" data-id="${a.id}">Отклонить</button><button type="button" class="btn btn-attention" data-action="approve" data-id="${a.id}">Разрешить</button></div>
      </div>` : `<p class="t-body">Нет ожидающих решений.</p>`}
    </div></main>`;
  } else if (route.name === 'bot-settings') {
    const bot = bots.find((b) => b.id === route.id);
    mainHtml = bot
      ? `<main class="desktop-main" data-bot="${esc(bot.id)}"><div class="desktop-thread-head">${avatarSlot(bot, 36)}<div class="flex-1 min-w-0 stack"><h1 class="t-headline header-title">${esc(bot.name)}</h1><span class="t-footnote">Настройки бота</span></div><a href="javascript:void(0)" data-action="open-thread" data-bot="${esc(bot.id)}" class="btn btn-secondary">К треду</a></div>
      <div class="desktop-thread-body" style="max-width:560px;gap:10px;">${botSettingsCards(bot)}</div></main>`
      : `<main class="desktop-main"><div class="center-screen"><p class="t-body">Бот не найден.</p><a class="btn btn-secondary" href="#/">На главную</a></div></main>`;
  } else {
    mainHtml = `<main class="desktop-main"><div class="center-screen"><p class="t-body">Бот не выбран. Нужный бот в списке слева.</p></div></main>`;
  }

  app.innerHTML = `<div class="desktop-shell">${SKIP_LINK}${sidebar}${mainHtml}${asideHtml}</div>`;
  const mainEl = app.querySelector('.desktop-main');
  mainEl.id = 'content';
  mainEl.tabIndex = -1;
  if (route.name === 'bot-settings') mountBotModel(app.querySelector('main[data-bot]'), route.id);
  if (route.name === 'browser') mountBrowser({ app, bot: bots.find((b) => b.id === route.id), botId: route.id, setCleanup });

  if (threadId) {
    const body = document.getElementById('thread-body');
    const thread = await api.getThread(threadId);
    const bot = bots.find((b) => b.id === thread?.bot_id) || {};
    if (hasBrowser(bot)) {
      const paint = (text) => { const node = app.querySelector('[data-browser-status]'); if (node) node.textContent = text; };
      api.getBrowser(bot.id).then((state) => paint(controlTitle(state.state))).catch(() => paint('Состояние экрана неизвестно'));
    }
    const stop = await startThreadStream(threadId, thread, body, bot.avatar);
    // Ввод открывается только после подключения к потоку треда: иначе событие отправленного сообщения приходит до подписки и теряется.
    app.querySelectorAll('#composer-input, [data-action="send-message"]').forEach((el) => { el.disabled = false; });
    setCleanup(stop);
  }
}

// ---------------------------------------------------------------------------
// Mobile dispatch
// ---------------------------------------------------------------------------
async function renderMobile(route) {
  if (route.name === 'main') return viewMain();
  if (route.name === 'thread') return viewThread(route.id);
  if (route.name === 'browser') return viewBrowser(route.id);
  if (route.name === 'approvals') return viewApprovals(route.id);
  if (route.name === 'routines') return viewRoutines();
  if (route.name === 'schedule') return viewSchedule(route.id);
  if (route.name === 'usage') return viewUsage();
  if (route.name === 'memory') return viewMemory();
  if (route.name === 'bot-settings') return viewBotSettings(route.id, route.qs);
  return viewMain();
}

// ---------------------------------------------------------------------------
// Service worker + push
// ---------------------------------------------------------------------------
if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('sw.js').catch(() => { /* офлайн-кэш необязателен */ });
  });
  navigator.serviceWorker.addEventListener('message', (event) => {
    if (event.data && event.data.type === 'navigate' && event.data.url) {
      const hash = event.data.url.split('#')[1];
      if (hash) location.hash = hash;
    }
  });
}

// Одноразовый #token=... в адресе (удобно на новом origin, где ещё нет
// localStorage-токена: печатать кириллицу/спецсимволы в адресную строку не
// нужно, а ввести токен через форму можно и после). Сохраняем и убираем из URL.
(function consumeTokenFromHash() {
  const m = location.hash.match(/^#\/?token=([^&]+)/);
  if (m) { api.setToken(decodeURIComponent(m[1])); location.hash = ''; }
})();

// Язык: каталог грузим до первой отрисовки; английский включает DOM-переводчик (i18n-dom.js), экраны остаются на русских строках.
// Смена языка перезагружает страницу: так весь интерфейс (включая то, что собрано при старте) перерисовывается заново.
await loadLang();
document.documentElement.lang = getLang();
startTranslator();
window.addEventListener('bothub:lang', () => location.reload());

render();
