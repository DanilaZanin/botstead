// cli-login.js: экран входа по подписке (docs/contracts.md §12). WebSocket /api/providers/{id}/login: бинарные кадры несут
// байты терминала, клиент шлёт байты ввода и JSON {t:'resize', cols, rows} / {t:'close'}; ядро присылает {t:'exit', code}.
// Над терминалом шапка «что сейчас происходит» по шагам, ссылка входа и поле кода вынесены в обычные элементы.
// В состояниях ошибки ссылка и поле кода прячутся (они устарели), остаётся одно главное действие.
import * as api from './api.js';
import { ICONS, esc } from './ui.js';
import { context, stateHtml, isDesktop } from './account.js';
import { CLI_LABEL } from './registry.js';
import { createTerminal, findLoginUrl, stripAnsi, applyCtrl, KEY_BYTES, ESCAPE_HATCH } from './terminal.js';

const HASH_LIST = '#/settings/providers';
const SUB_LABEL = { claude: 'Claude', codex: 'ChatGPT', agy: 'Google' };
const STEP_NAMES = ['Запуск входа на сервере', 'Открыть ссылку и войти в аккаунт', 'Вставить код из браузера'];
// Состояния, когда сессия на сервере закончилась: терминал неактивен, ссылка и поле кода устарели.
const ENDED = ['failed', 'lost', 'busy', 'forbidden', 'revoked', 'timeout', 'error'];
const encoder = new TextEncoder();
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const $ = (selector, root = document) => root.querySelector(selector);

function keyButtons() {
  const key = (label, data, aria = '') => `<button type="button" class="term-key" data-key="${esc(data)}"${aria ? ` aria-label="${aria}"` : ''}>${label}</button>`;
  return `<div class="term-keys" role="toolbar" aria-label="Клавиши терминала">
    ${key('Esc', 'Escape')}${key('Tab', 'Tab')}
    <button type="button" class="term-key" data-ctrl aria-pressed="false">Ctrl</button>
    ${key('←', 'ArrowLeft', 'Стрелка влево')}${key('↑', 'ArrowUp', 'Стрелка вверх')}${key('↓', 'ArrowDown', 'Стрелка вниз')}${key('→', 'ArrowRight', 'Стрелка вправо')}
    <button type="button" class="term-key" data-paste-term>${ICONS.clipboard}Вставить</button>
  </div>`;
}

export async function viewProviderLogin(providerId) {
  const c = context();
  let provider = null;
  try { provider = (await api.listProviders()).find((p) => p.id === providerId) || null; } catch (err) {
    if (err.status === 401) return;
    await c.frame({ title: 'Вход по подписке', subtitle: 'Провайдеры', backHref: HASH_LIST, body: `<div class="page-narrow">${stateHtml({ iconHtml: ICONS.cloudOff, title: 'Данные провайдера не загрузились', text: 'Терминал не запущен. Проверьте сеть или VPN.', actions: '<button type="button" class="btn btn-secondary" data-act="retry">Повторить</button>', kind: 'state-error' })}</div>` });
    $('[data-act="retry"]')?.addEventListener('click', () => c.rerender());
    return;
  }
  if (!provider || provider.kind !== 'cli_subscription') {
    await c.frame({ title: 'Вход по подписке', subtitle: 'Провайдеры', backHref: HASH_LIST, body: `<div class="page-narrow">${stateHtml({ iconHtml: ICONS.lock, title: 'Нет подписочного провайдера', text: 'Вход через терминал работает только для провайдеров-подписок. Возможно, провайдер уже удалён.', actions: `<a class="btn btn-secondary" href="${HASH_LIST}">К провайдерам</a>` })}</div>` });
    return;
  }

  const desktop = isDesktop();
  const cli = provider.cli;
  const title = `Вход: ${CLI_LABEL[cli] || cli}`;
  const subtitle = `Подписка ${SUB_LABEL[cli] || ''}`.trim();
  const providerHref = `${HASH_LIST}/${providerId}`;

  // Подсказка про клавиатуру нужна только на компьютере: на телефоне нет Shift+Esc, есть ряд клавиш.
  const termHtml = `<div class="term-wrap"><div id="cl-term" class="term-host" data-i18n-skip role="log" aria-label="Терминал входа" aria-live="off"></div>
    ${desktop ? `<p class="t-footnote term-hint" id="cl-kbd-hint">Терминал открывается кликом, Tab в него не заходит. Esc уходит в команду. Выйти из терминала с клавиатуры: ${ESCAPE_HATCH}.</p>` : ''}
    <p class="t-footnote term-hint" id="cl-save-note">Содержимое окна нигде не сохраняется. Данные входа хранятся на сервере отдельно для каждого пользователя и доступны только его ботам.</p></div>`;
  const linkHtml = `<div id="cl-link-card" class="card card-pad stack gap-2" hidden>
    <span class="t-footnote">Ссылка для входа</span>
    <span class="t-log cl-link-text" id="cl-link-text" data-i18n-skip></span>
    <div class="cl-link-actions"><a id="cl-open" class="btn btn-primary" href="#" target="_blank" rel="noopener noreferrer">${ICONS.external}Открыть страницу входа</a><button type="button" id="cl-copy" class="btn btn-secondary">${ICONS.copy}Скопировать ссылку</button></div>
  </div>`;
  const codeHtml = `<form id="cl-form" class="cl-code stack gap-2" novalidate autocomplete="off">
    <label for="cl-code">Код из браузера</label>
    <div class="input-row"><input id="cl-code" class="input mono" type="text" autocomplete="off" autocapitalize="none" autocorrect="off" spellcheck="false" aria-describedby="cl-code-note"><button type="button" class="icon-btn sunken" id="cl-code-paste" aria-label="Вставить код из буфера">${ICONS.clipboard}</button><button type="submit" class="btn btn-primary" id="cl-send" disabled>Отправить</button></div>
    <span id="cl-code-note" class="field-note" aria-live="polite">Страница входа покажет код. Вставьте его сюда.</span>
  </form>`;
  const statusHtml = '<div id="cl-status" class="banner banner-info" role="status"></div>';
  const stepsHtml = `<ol id="cl-steps" class="cl-steps">${STEP_NAMES.map((name, i) => `<li data-step="${i + 1}"><span class="cl-step-mark" aria-hidden="true"></span><span>${esc(name)}</span></li>`).join('')}</ol>`;
  const actionsHtml = '<div id="cl-actions" class="stack gap-2"></div>';
  const resultHtml = '<div id="cl-result" class="stack gap-2" aria-live="polite"></div>';
  const closeLink = `<a id="cl-close" class="btn btn-secondary" href="${HASH_LIST}">Закрыть</a>`;

  if (desktop) {
    await c.frame({
      title, subtitle, backHref: providerHref,
      body: `<div class="cl-desktop-body">${termHtml}</div>`,
      desktopAside: `<div class="stack gap-3"><h2 class="desktop-aside-label">Что происходит</h2>${statusHtml}${stepsHtml}${linkHtml}${codeHtml}${resultHtml}${actionsHtml}</div>`,
    });
  } else {
    c.app.innerHTML = `<div class="screen cl-screen">
      <header class="app-header">
        <a id="cl-close" href="${HASH_LIST}" aria-label="Закрыть терминал" class="icon-btn">${ICONS.closeLg}</a>
        <div class="flex-1 min-w-0 stack"><h1 class="t-headline header-title">${esc(title)}</h1><span class="t-footnote cl-sub">${esc(subtitle)}</span></div>
      </header>
      <div class="cl-banner">${statusHtml}</div>
      <div class="cl-term-area">${termHtml}</div>
      <div class="cl-below stack gap-3">${linkHtml}${codeHtml}${resultHtml}${actionsHtml}</div>
      ${keyButtons()}
    </div>`;
  }

  // ---- состояние экрана ----
  const st = { phase: 'connecting', link: '', opened: false, exitCode: null, socket: null, open: false, size: null, ctrl: false, failStep: 0, background: false, linkFocused: false, retryLabel: '' };
  let term = null;
  let disposed = false;
  let tail = '';
  const decoder = new TextDecoder();
  const host = $('#cl-term');
  host.tabIndex = -1;
  const statusEl = $('#cl-status');
  const actionsEl = $('#cl-actions');
  const resultEl = $('#cl-result');
  const codeInput = $('#cl-code');
  const sendBtn = $('#cl-send');
  const linkCard = $('#cl-link-card');
  const keysEl = $('.term-keys');

  function step() {
    if (st.phase === 'connecting' || !st.link) return 1;
    return st.opened ? 3 : 2;
  }
  function describe() {
    const n = step();
    switch (st.phase) {
      case 'connecting': return { kind: 'info', spin: true, title: 'Шаг 1 из 3. Запускаю терминал', text: 'Готовлю вход на сервере.' };
      case 'waiting':
        if (!st.link) return { kind: 'info', spin: true, title: 'Шаг 1 из 3. Жду ссылку для входа', text: 'Терминал запущен, ссылка появится в выводе.' };
        if (n === 2) return { kind: 'attention', title: 'Шаг 2 из 3. Откройте ссылку и войдите', text: 'Сайт покажет код. Шаг 3: вставить его в поле «Код из браузера».' };
        return { kind: 'attention', title: 'Шаг 3 из 3. Вставьте код из браузера', text: 'Код действует несколько минут и только один раз.' };
      case 'checking': return { kind: 'info', spin: true, title: 'Проверяю вход', text: 'Сервер проверяет подписку и обновляет список моделей.' };
      case 'done': return { kind: 'success', title: 'Вход выполнен', text: `${CLI_LABEL[cli] || cli} подключён. Окно входа можно закрыть, вход сохранён на сервере.` };
      case 'failed': return { kind: 'danger', title: st.reason || 'Вход не завершён', text: st.reasonText || 'Начните вход заново.' };
      case 'lost':
        if (st.background) return { kind: 'danger', title: 'Связь прервалась, пока приложение было свёрнуто', text: `Вход не завершён: сессия на сервере закрыта вместе с соединением.${codeInput.value.trim() ? ' Код в поле сохранён, но после нового подключения ссылка будет другой: если код не подойдёт, получите новый.' : ''} Нажмите «Подключиться снова».` };
        return { kind: 'danger', title: 'Связь с терминалом потеряна', text: 'Вход не завершён. Сессия на сервере закрыта вместе с соединением, вход нужно начать заново.' };
      case 'busy': return { kind: 'danger', title: 'Вход уже открыт в другом месте', text: 'Закройте терминал входа на другом устройстве или во вкладке и повторите. Одновременно открыт один вход на подписку.' };
      case 'forbidden': return { kind: 'danger', title: 'Нет доступа к этому входу', text: 'Провайдер не найден, это не подписка, либо вход на сервере сейчас недоступен.' };
      case 'revoked': return { kind: 'danger', title: 'Сессия botstead закрыта', text: 'Сессию отозвали или пользователя отключили. Войдите в botstead заново.' };
      case 'timeout': return { kind: 'danger', title: 'Сессия закрыта по времени', text: 'Терминал входа живёт до 30 минут и закрывается после 10 минут без ввода. Начните вход заново.' };
      default: return { kind: 'danger', title: 'Терминал не запустился', text: 'Сервер не смог открыть вход. Попробуйте позже.' };
    }
  }
  const BANNER_CLASS = { info: 'banner-info', attention: 'banner-attention', danger: 'banner-danger', success: 'banner-success' };

  function paintSteps() {
    const list = $('#cl-steps');
    if (!list) return;
    const n = st.phase === 'done' || st.phase === 'checking' ? 4 : step();
    list.querySelectorAll('li').forEach((li) => {
      const i = Number(li.getAttribute('data-step'));
      let state = i < n ? 'done' : i === n ? 'active' : 'todo';
      if (st.phase === 'failed' && i === st.failStep) state = 'failed';
      if (st.phase === 'failed' && i < st.failStep) state = 'done';
      if (st.phase === 'failed' && i > st.failStep) state = 'todo';
      if (ENDED.includes(st.phase) && st.phase !== 'failed' && i === n) state = 'stopped';
      li.setAttribute('data-state', state);
      const names = { done: 'готово', active: 'идёт', todo: 'в очереди', failed: 'сбой', stopped: 'остановлено' };
      li.setAttribute('aria-label', `Шаг ${i}: ${names[state]}`);
      $('.cl-step-mark', li).innerHTML = state === 'done' ? ICONS.check : state === 'active' ? `<span class="spin">${ICONS.spinner}</span>` : state === 'failed' ? ICONS.alert : String(i);
    });
  }

  function paintActions() {
    const phase = st.phase;
    const closeBtn = (cls) => (desktop ? closeLink.replace('btn-secondary', cls) : `<a class="btn ${cls}" href="${HASH_LIST}">Закрыть</a>`);
    const restart = (label) => `<button type="button" class="btn btn-primary" data-act="restart">${ICONS.retry}${label}</button>`;
    let html;
    if (phase === 'done') html = `<a class="btn btn-primary" href="${providerHref}">Выбрать модели</a>${closeBtn('btn-secondary')}`;
    else if (phase === 'failed') html = `${restart(st.retryLabel || 'Начать заново')}${closeBtn('btn-secondary')}`;
    else if (phase === 'timeout' || phase === 'error') html = `${restart('Начать заново')}${closeBtn('btn-secondary')}`;
    else if (phase === 'lost') html = `${restart('Подключиться снова')}${closeBtn('btn-secondary')}`;
    else if (phase === 'busy') html = `${restart('Повторить')}${closeBtn('btn-secondary')}`;
    else if (phase === 'forbidden' || phase === 'revoked') html = closeBtn('btn-primary');
    else html = desktop ? closeLink : `<a class="btn btn-ghost" href="${HASH_LIST}">Отмена</a>`;
    actionsEl.innerHTML = html;
    if (!desktop && phase === 'done') actionsEl.classList.add('btn-row-2'); else actionsEl.classList.remove('btn-row-2');
  }

  function paint() {
    const d = describe();
    statusEl.className = `banner ${BANNER_CLASS[d.kind]}`;
    statusEl.setAttribute('role', d.kind === 'danger' ? 'alert' : 'status');
    statusEl.innerHTML = `<span class="banner-icon${d.spin ? ' spin' : ''}" aria-hidden="true">${d.spin ? ICONS.spinner : d.kind === 'success' ? ICONS.check : d.kind === 'info' ? ICONS.terminal : ICONS.alert}</span><span class="banner-text"><span class="banner-title">${esc(d.title)}</span><span class="banner-sub">${esc(d.text)}</span></span>`;
    const ended = ENDED.includes(st.phase);
    host.classList.toggle('is-muted', ended && st.phase !== 'failed');
    host.setAttribute('data-phase', st.phase);
    sendBtn.disabled = !st.open || !codeInput.value.trim();
    // Вход выполнен или сессия закончилась: ссылка и поле кода уже не нужны. Введённый код остаётся в скрытом поле.
    const finished = ended || st.phase === 'done';
    $('#cl-form').hidden = finished;
    linkCard.hidden = finished || !st.link;
    if (keysEl) keysEl.hidden = finished;
    paintSteps();
    paintActions();
  }
  function setPhase(phase, extra = {}) {
    if (disposed) return;
    Object.assign(st, extra);
    st.phase = phase;
    if (phase === 'failed' && !st.failStep) st.failStep = step();
    paint();
    if (phase === 'revoked') api.authMe().catch((err) => { if (err.status === 401) window.dispatchEvent(new CustomEvent('bothub-unauthorized')); });
  }

  // ---- ввод ----
  function sendInput(data, { raw = false } = {}) {
    if (!st.open || !st.socket) return;
    let out = data;
    if (st.ctrl && !raw) { out = applyCtrl(data); setCtrl(false); }
    st.socket.send(encoder.encode(out));
  }
  function setCtrl(on) {
    st.ctrl = on;
    const btn = $('[data-ctrl]');
    if (btn) { btn.setAttribute('aria-pressed', String(on)); btn.classList.toggle('is-on', on); }
  }
  function sendResize() {
    if (!st.open || !st.size) return;
    st.socket.send(JSON.stringify({ t: 'resize', cols: st.size.cols, rows: st.size.rows }));
  }

  // ---- вывод: ссылка входа ----
  function scan(bytes) {
    tail = (tail + stripAnsi(decoder.decode(bytes, { stream: true }))).slice(-4000);
    const url = findLoginUrl(tail);
    if (!url || url === st.link) return;
    st.link = url;
    let shown = url;
    try { const u = new URL(url); shown = `${u.host}${u.pathname}`; } catch { /* оставляем как есть */ }
    $('#cl-link-text').textContent = shown.length > 56 ? `${shown.slice(0, 55)}…` : shown;
    const open = $('#cl-open');
    open.href = url;
    paint();
    // На компьютере фокус переходит на главное действие: с клавиатуры это один Enter. В терминал Tab не заходит.
    if (desktop && !st.linkFocused && !linkCard.hidden) { st.linkFocused = true; open.focus(); }
  }

  // ---- подключение ----
  function onClose(socket, code, wasOpen, exited) {
    if (st.socket !== socket) return;
    st.open = false;
    if (exited || st.phase === 'checking' || st.phase === 'done' || st.phase === 'failed') return;
    if (code === 4409) setPhase('busy');
    else if (code === 4404) setPhase('forbidden');
    else if (code === 4401) setPhase('revoked');
    else if (code === 1001) setPhase('timeout');
    else if (code === 1011 || code === 1009 || code === 1003) setPhase('error');
    else if (wasOpen) setPhase('lost', { background: document.visibilityState === 'hidden' || st.background });
    else setPhase('error');
  }

  function connect({ keepCode = false } = {}) {
    const previous = st.socket;
    if (previous) {
      st.socket = null;
      previous.onmessage = previous.onclose = previous.onerror = previous.onopen = null;
      try { previous.close(); } catch { /* уже закрыт */ }
    }
    st.open = false;
    st.phase = 'connecting';
    st.link = '';
    st.opened = false;
    st.exitCode = null;
    st.failStep = 0;
    st.reason = '';
    st.reasonText = '';
    st.retryLabel = '';
    st.background = false;
    st.linkFocused = false;
    tail = '';
    resultEl.innerHTML = '';
    linkCard.hidden = true;
    if (!keepCode) codeInput.value = '';
    $('#cl-code-note').textContent = 'Страница входа покажет код. Вставьте его сюда.';
    setCtrl(false);
    paint();
    let socket;
    try { socket = api.openLoginSocket(providerId); } catch { setPhase('error'); return; }
    st.socket = socket;
    let wasOpen = false;
    let exited = false;
    socket.onopen = () => {
      if (st.socket !== socket) return;
      wasOpen = true;
      st.open = true;
      setPhase('waiting');
      term.fit();
      sendResize();
    };
    socket.onmessage = (event) => {
      if (st.socket !== socket || disposed) return;
      if (typeof event.data === 'string') {
        let frame = null;
        try { frame = JSON.parse(event.data); } catch { return; }
        if (frame && frame.t === 'exit') { exited = true; st.exitCode = Number(frame.code); finish(); }
        return;
      }
      const bytes = new Uint8Array(event.data);
      term.write(bytes);
      scan(bytes);
    };
    socket.onclose = (event) => onClose(socket, event.code, wasOpen, exited);
    socket.onerror = () => { /* за ошибкой всегда идёт close с кодом */ };
  }

  // После выхода ядро отдельно проверяет подписку и обновляет статус провайдера: ждём новую отметку проверки.
  async function finish() {
    if (st.exitCode !== 0) {
      // Выход после отправки кода значит, что код не подошёл (просрочен или уже использован).
      if (st.opened) setPhase('failed', { failStep: 3, reason: 'Код не подошёл', reasonText: 'Код действует несколько минут и только один раз. Получите новый код: вход откроется заново, на странице входа появится свежий код.', retryLabel: 'Получить новый код' });
      else setPhase('failed', { failStep: step(), reason: 'Вход не завершён', reasonText: 'Вход на сервере завершился с ошибкой. Начните вход заново.', retryLabel: 'Начать заново' });
      return;
    }
    setPhase('checking');
    const before = provider.last_check_at;
    let latest = null;
    for (let i = 0; i < 20 && !disposed; i++) {
      await sleep(i === 0 ? 700 : 1000);
      try {
        const found = (await api.listProviders()).find((p) => p.id === providerId);
        if (found && found.last_check_at !== before) { latest = found; break; }
      } catch (err) { if (err.status === 401) return; }
    }
    if (disposed) return;
    if (!latest || latest.status !== 'ok') {
      setPhase('failed', { failStep: 3, retryLabel: 'Начать заново', reason: 'Вход не подтвердился', reasonText: latest ? 'Команда завершилась, но проверка подписки не прошла. Начните вход заново.' : 'Сервер не успел проверить вход. Откройте провайдера и нажмите «Проверить».' });
      return;
    }
    provider = latest;
    setPhase('done');
    // Фокус на итог: иначе он остаётся на скрытом поле или в терминале, а результат читается только глазами.
    statusEl.tabIndex = -1;
    statusEl.focus();
    await report();
  }

  async function report() {
    let models = null;
    try { models = (await api.listModels(true)).filter((m) => m.provider_id === providerId && m.enabled).length; } catch { /* без числа */ }
    let bots = [];
    for (let i = 0; i < 2 && !disposed; i++) {
      await sleep(i === 0 ? 600 : 1500);
      try { bots = (await api.listBots()).filter((b) => b.provider_id === providerId); } catch { break; }
      if (!bots.some((b) => b.need_restart)) break;
    }
    if (disposed) return;
    const later = bots.filter((b) => b.need_restart).map((b) => b.name);
    const done = bots.filter((b) => !b.need_restart).map((b) => b.name);
    const lines = [];
    if (models !== null) lines.push(`Найдено моделей: ${models}.`);
    if (done.length) lines.push(`Перезапущены ${done.length === 1 ? 'бот' : 'боты'}: ${done.join(', ')}.`);
    if (later.length) lines.push(`Перезапустятся после текущей задачи: ${later.join(', ')}.`);
    if (!bots.length) lines.push('Ботов с этим провайдером пока нет.');
    resultEl.innerHTML = `<div class="banner banner-info" role="status"><span class="banner-text"><span class="banner-sub">${lines.map(esc).join(' ')}</span></span></div>`;
  }

  // ---- события экрана ----
  actionsEl.addEventListener('click', (e) => {
    if (e.target.closest('[data-act="restart"]')) { const keepCode = st.phase === 'lost'; term.focus(); connect({ keepCode }); }
  });
  $('#cl-form').addEventListener('submit', (e) => {
    e.preventDefault();
    const code = codeInput.value.trim();
    if (!code || !st.open) return;
    sendInput(`${code}\r`, { raw: true });
    codeInput.value = '';
    st.opened = true;
    $('#cl-code-note').textContent = 'Код отправлен. Жду ответ терминала.';
    paint();
  });
  codeInput.addEventListener('input', () => { sendBtn.disabled = !st.open || !codeInput.value.trim(); });
  codeInput.addEventListener('focus', () => { if (st.link && !st.opened) { st.opened = true; paint(); } });
  $('#cl-code-paste').addEventListener('click', async () => {
    try { codeInput.value = (await navigator.clipboard.readText()).trim(); codeInput.dispatchEvent(new Event('input')); codeInput.focus(); } catch {
      $('#cl-code-note').textContent = 'Не удалось прочитать буфер: вставьте код вручную.';
    }
  });
  $('#cl-open').addEventListener('click', () => { if (!st.opened) { st.opened = true; paint(); } });
  $('#cl-copy').addEventListener('click', async () => {
    const note = $('#cl-code-note');
    try { await navigator.clipboard.writeText(st.link); note.textContent = 'Ссылка скопирована.'; } catch { note.textContent = 'Не удалось скопировать ссылку: откройте её кнопкой выше.'; }
  });
  const keys = $('.term-keys');
  if (keys) {
    // Кнопки не забирают фокус у терминала: экранная клавиатура остаётся на месте.
    keys.addEventListener('pointerdown', (e) => { if (e.target.closest('button')) e.preventDefault(); });
    keys.addEventListener('mousedown', (e) => { if (e.target.closest('button')) e.preventDefault(); });
    keys.addEventListener('click', async (e) => {
      const btn = e.target.closest('button');
      if (!btn) return;
      if (btn.hasAttribute('data-ctrl')) { setCtrl(!st.ctrl); return; }
      if (btn.hasAttribute('data-paste-term')) {
        try { const text = await navigator.clipboard.readText(); if (text) sendInput(text, { raw: true }); } catch {
          $('#cl-code-note').textContent = 'Не удалось прочитать буфер: вставьте код в поле «Код из браузера».';
        }
        return;
      }
      const data = KEY_BYTES[btn.getAttribute('data-key')];
      if (data) sendInput(data);
    });
  }

  term = await createTerminal(host, {
    mobile: !desktop,
    onData: (data) => sendInput(data),
    onResize: (cols, rows) => { st.size = { cols, rows }; sendResize(); },
    onEscape: () => (document.getElementById('cl-close') || actionsEl.querySelector('a, button'))?.focus(),
  });
  host.addEventListener('click', () => term.focus());
  const observer = new ResizeObserver(() => { clearTimeout(observer.timer); observer.timer = setTimeout(() => { if (!disposed) term.fit(); }, 120); });
  observer.observe(host);
  // Первый fit только после раскладки и загрузки шрифтов: иначе на телефоне серверу уходит размер по умолчанию (80 колонок).
  try { await document.fonts.ready; } catch { /* без шрифтов считаем по тому, что есть */ }
  await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  if (disposed) return;
  term.fit();
  connect();
  if (!desktop) term.focus();

  // Возврат из браузера: на телефоне страница засыпает, и соединение молча обрывается. При возврате проверяем сокет
  // и честно говорим, что связь прервалась, вместо терминала, который выглядит живым.
  const onVisible = () => {
    if (disposed || document.visibilityState !== 'visible') return;
    const socket = st.socket;
    if (st.phase === 'waiting' && socket && socket.readyState > 1) {
      st.open = false;
      setPhase('lost', { background: true });
    }
  };
  document.addEventListener('visibilitychange', onVisible);
  window.addEventListener('pageshow', onVisible);

  // Клавиатура телефона: экран занимает видимую часть окна, терминал и поле кода не уезжают под клавиатуру.
  const vv = window.visualViewport;
  const screenEl = $('.cl-screen');
  const onViewport = () => {
    if (!screenEl || !vv) return;
    screenEl.style.setProperty('--cl-vh', `${Math.round(vv.height)}px`);
    window.scrollTo(0, 0);
  };
  if (!desktop && vv) {
    vv.addEventListener('resize', onViewport);
    vv.addEventListener('scroll', onViewport);
    onViewport();
  }

  c.setCleanup(() => {
    disposed = true;
    document.removeEventListener('visibilitychange', onVisible);
    window.removeEventListener('pageshow', onVisible);
    if (vv) { vv.removeEventListener('resize', onViewport); vv.removeEventListener('scroll', onViewport); }
    clearTimeout(observer.timer);
    observer.disconnect();
    const socket = st.socket;
    st.socket = null;
    if (socket) {
      socket.onmessage = socket.onclose = socket.onerror = socket.onopen = null;
      try { if (st.open) socket.send(JSON.stringify({ t: 'close' })); socket.close(); } catch { /* уже закрыт */ }
    }
    st.open = false;
    tail = '';
    term.dispose();
  });
}

