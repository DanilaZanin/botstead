// account.js: экраны входа, первичной настройки, инвайта, настроек, пользователей и сессий.
// app.js передаёт контекст через createAccount(): каркас экрана (frame), текущую сессию и переходы.
import * as api from './api.js';
import { ICONS, esc, plural, fmtDate, fmtDateTime, alertHtml } from './ui.js';
import { usableCount } from './registry.js';
import { LANGS, getLang, setLang } from './i18n.js';

let ctx = null;
const NOTES_KEY = 'bothub_invite_notes';
const WELCOME_KEY = 'bothub_welcome';
let welcomeFlag = false;

// Экран #/welcome только сразу после регистрации или первичной настройки: признак живёт в sessionStorage и снимается при показе.
function markWelcome() {
  welcomeFlag = true;
  try { sessionStorage.setItem(WELCOME_KEY, '1'); } catch { /* приватный режим: хватит переменной */ }
}
export function takeWelcome() {
  let stored = false;
  try { stored = sessionStorage.getItem(WELCOME_KEY) === '1'; sessionStorage.removeItem(WELCOME_KEY); } catch { /* память недоступна */ }
  const allowed = stored || welcomeFlag;
  welcomeFlag = false;
  return allowed;
}

// Каркас экрана, сессия и переходы для соседних модулей (providers.js, cli-login.js).
export function context() { return ctx; }

export function createAccount(context) {
  ctx = context;
  return {
    viewLogin, viewTokenGate, viewSetup, viewInvite, viewWelcome,
    viewSettings, viewUsers, viewPassword, viewSessions, viewStub, takeWelcome, viewOffline,
  };
}

// ---------------------------------------------------------------------------
// Общие кирпичики форм
// ---------------------------------------------------------------------------

const $ = (selector, root = document) => root.querySelector(selector);

export function field({ id, label, type = 'text', value = '', autocomplete = 'off', hint = '', toggle = false, inputmode = '', mono = false, placeholder = '', describedBy = '', after = '', required = true }) {
  return `<div class="form-field">
    <label for="${id}">${esc(label)}</label>
    <div class="input-row">
      <input id="${id}" name="${id}" class="input${mono ? ' mono' : ''}" type="${type}" value="${esc(value)}" autocomplete="${autocomplete}" autocapitalize="none" autocorrect="off" spellcheck="false"${required ? ' aria-required="true"' : ''} aria-describedby="${id}-note${describedBy ? ` ${describedBy}` : ''}"${inputmode ? ` inputmode="${inputmode}"` : ''}${placeholder ? ` placeholder="${esc(placeholder)}"` : ''}>
      ${toggle ? `<button type="button" class="icon-btn" data-toggle="${id}" aria-label="Показать пароль" aria-pressed="false">${ICONS.eye}</button>` : ''}
      ${after}
    </div>
    <span id="${id}-note" class="field-note" data-hint="${esc(hint)}" aria-live="polite">${esc(hint)}</span>
  </div>`;
}

function wireToggles(root) {
  root.querySelectorAll('[data-toggle]').forEach((button) => {
    button.addEventListener('click', () => {
      const input = document.getElementById(button.getAttribute('data-toggle'));
      const show = input.type === 'password';
      input.type = show ? 'text' : 'password';
      button.setAttribute('aria-pressed', String(show));
      button.setAttribute('aria-label', show ? 'Скрыть пароль' : 'Показать пароль');
      button.innerHTML = show ? ICONS.eyeOff : ICONS.eye;
    });
  });
}

export function setFieldError(input, message) {
  input.setAttribute('aria-invalid', 'true');
  const note = document.getElementById(`${input.id}-note`);
  note.textContent = message;
  note.classList.add('is-error');
}

export function clearErrors(root) {
  root.querySelectorAll('[aria-invalid]').forEach((el) => el.removeAttribute('aria-invalid'));
  root.querySelectorAll('.field-note').forEach((note) => {
    note.textContent = note.getAttribute('data-hint') || '';
    note.classList.remove('is-error');
  });
  root.querySelectorAll('.form-alert').forEach((box) => { box.innerHTML = ''; });
}

// Плашка ошибки с role=alert; фокус уходит на неё, чтобы сообщение не потерялось после сбоя.
export function setAlert(box, title, text = '', kind = 'danger') {
  box.innerHTML = title ? alertHtml(title, text, kind) : '';
  if (title) box.firstElementChild.focus();
}

export function setBusy(button, busy, busyText, idleHtml) {
  button.disabled = busy;
  if (busy) {
    button.setAttribute('aria-busy', 'true');
    button.innerHTML = `<span class="spin">${ICONS.spinner}</span>${esc(busyText)}`;
  } else {
    button.removeAttribute('aria-busy');
    button.innerHTML = idleHtml;
  }
}

function lockForm(form, locked) {
  form.querySelectorAll('input, [data-toggle]').forEach((el) => { el.disabled = locked; });
}

// Ошибка сети: у fetch нет status. Остальное разбираем по коду ответа.
function isNetworkError(err) { return !err || !err.status; }

// Текст для сбоя без понятного ответа сервера: код ответа пользователю не показываем.
export function failure(err, unsent = '') {
  return { title: 'Сервер не отвечает', text: `${unsent}${isNetworkError(err) ? 'Проверьте сеть или VPN.' : 'Попробуйте ещё раз через минуту.'}` };
}
function failureAlert(box, err, unsent = '') {
  const { title, text } = failure(err, unsent);
  setAlert(box, title, text);
}

export function loadingHtml(text) {
  return `<div class="state-loading" role="status" aria-busy="true"><span class="state-loading-row"><span class="spin">${ICONS.spinner}</span>${esc(text)}</span>
    ${'<span class="skeleton-row"><span class="skeleton"></span><span class="skeleton short"></span></span>'.repeat(3)}</div>`;
}

export function stateHtml({ iconHtml, title, text, actions = '', kind = '' }) {
  return `<div class="state-box ${kind}"${kind === 'state-error' ? ' role="alert"' : ''}>
    <span class="state-icon" aria-hidden="true">${iconHtml}</span>
    <h2 class="state-title">${esc(title)}</h2>
    <p class="t-footnote state-text">${esc(text)}</p>
    ${actions ? `<div class="state-actions">${actions}</div>` : ''}
  </div>`;
}

export const retryButton = `<button type="button" class="btn btn-secondary" data-act="retry">${ICONS.retry}Повторить</button>`;

// Оболочка экранов без шапки: вход, настройка, инвайт, первый вход.
function authShell(inner) {
  ctx.app.innerHTML = `<div class="auth-screen"><main class="auth-main">${inner}</main></div>`;
}

// ---------------------------------------------------------------------------
// Диалог: лист снизу на телефоне, окно по центру на Mac
// ---------------------------------------------------------------------------

export function openDialog({ title, subtitle = '', content = '', focus = 'first' }) {
  const trigger = document.activeElement;
  const root = document.createElement('div');
  root.className = 'overlay';
  root.innerHTML = `<div class="overlay-backdrop" data-close></div>
    <section class="dialog" role="dialog" aria-modal="true" aria-labelledby="dlg-title" tabindex="-1">
      <div class="sheet-grabber"></div>
      <header class="dialog-head">
        <div class="stack flex-1 min-w-0"><h2 id="dlg-title" class="h-title dialog-title">${esc(title)}</h2>${subtitle ? `<span class="t-footnote" data-dlg-sub>${esc(subtitle)}</span>` : ''}</div>
        <button type="button" class="icon-btn" aria-label="Закрыть" data-close>${ICONS.closeLg}</button>
      </header>
      <div class="dialog-body">${content}</div>
    </section>`;
  const siblings = Array.from(ctx.app.children);
  ctx.app.appendChild(root);
  siblings.forEach((el) => { el.inert = true; });
  const dialog = $('.dialog', root);
  let closed = false;
  const api_ = {
    el: root,
    body: $('.dialog-body', root),
    setHeader(newTitle, newSubtitle = '') {
      $('#dlg-title', root).textContent = newTitle;
      let sub = $('[data-dlg-sub]', root);
      if (!sub && newSubtitle) {
        sub = document.createElement('span');
        sub.className = 't-footnote';
        sub.setAttribute('data-dlg-sub', '');
        $('#dlg-title', root).after(sub);
      }
      if (sub) sub.textContent = newSubtitle;
    },
    setBody(html, mode = 'first') { api_.body.innerHTML = html; focusFirst(mode); },
    close() {
      if (closed) return;
      closed = true;
      document.removeEventListener('keydown', onKey);
      root.remove();
      siblings.forEach((el) => { el.inert = false; });
      if (trigger && trigger.isConnected && trigger.focus) trigger.focus();
      if (api_.onClose) api_.onClose();
    },
  };
  function focusFirst(mode = 'first') {
    const first = mode === 'first' ? api_.body.querySelector('input, button, [href]') : null;
    (first || dialog).focus();
  }
  root.addEventListener('click', (e) => { if (e.target.closest('[data-close]')) api_.close(); });
  // На документе, а не на диалоге: после блокировки кнопок во время запроса фокус уходит на body.
  const onKey = (e) => { if (e.key === 'Escape') api_.close(); };
  document.addEventListener('keydown', onKey);
  focusFirst(focus);
  return api_;
}

// Подтверждение опасного действия: лист снизу на телефоне, окно на Mac. Фокус на «Отмена», а не на опасной кнопке.
export function confirmBody({ text, confirmLabel, danger = true, cancelLabel = 'Отмена', cancelAttr = 'data-close' }) {
  return `<p class="t-body confirm-text">${esc(text)}</p>
    <div class="form-alert" id="cf-alert"></div>
    <div class="btn-row btn-row-2"><button type="button" class="btn btn-secondary" ${cancelAttr}>${esc(cancelLabel)}</button><button type="button" class="btn ${danger ? 'btn-danger' : 'btn-primary'}" data-confirm>${esc(confirmLabel)}</button></div>`;
}

// run() выполняет действие; при ошибке describeError даёт текст, диалог остаётся открытым.
export function wireConfirm(dlg, { run, describeError, done }) {
  const button = $('[data-confirm]', dlg.el);
  button.addEventListener('click', async () => {
    dlg.body.querySelectorAll('button').forEach((b) => { b.disabled = true; });
    try {
      await run();
    } catch (err) {
      dlg.body.querySelectorAll('button').forEach((b) => { b.disabled = false; });
      const { title, text } = describeError(err);
      setAlert($('#cf-alert', dlg.el), title, text);
      return;
    }
    dlg.close();
    if (done) done();
  });
}

export function confirmAction({ title, subtitle = '', text, confirmLabel, danger = true, run, describeError = userPatchError, done }) {
  const dlg = openDialog({ title, subtitle, content: confirmBody({ text, confirmLabel, danger }) });
  wireConfirm(dlg, { run, describeError, done });
  return dlg;
}

// data-seg хранит исходное русское имя группы: aria-label при английском интерфейсе переводится, поиск группы идёт по data-seg.
// skip: подписи кнопок не переводятся (названия языков).
export function segmentedHtml(label, options, selected, { skip = false } = {}) {
  return `<div role="radiogroup" aria-label="${esc(label)}" data-seg="${esc(label)}" class="segmented" style="grid-template-columns:repeat(${options.length},minmax(0,1fr));">${options.map((o) => `<button type="button" role="radio"${skip ? ' data-i18n-skip' : ''} data-value="${esc(o.value)}" aria-checked="${o.value === String(selected)}" tabindex="${o.value === String(selected) ? 0 : -1}">${esc(o.label)}</button>`).join('')}</div>`;
}

export function wireSegmented(root) {
  root.querySelectorAll('[role="radiogroup"]').forEach((group) => {
    const buttons = Array.from(group.querySelectorAll('[role="radio"]'));
    const pick = (button) => {
      buttons.forEach((b) => { b.setAttribute('aria-checked', String(b === button)); b.tabIndex = b === button ? 0 : -1; });
      button.focus();
    };
    buttons.forEach((button, i) => {
      button.addEventListener('click', () => pick(button));
      button.addEventListener('keydown', (e) => {
        if (e.key === 'ArrowRight' || e.key === 'ArrowDown') { e.preventDefault(); pick(buttons[(i + 1) % buttons.length]); }
        if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') { e.preventDefault(); pick(buttons[(i - 1 + buttons.length) % buttons.length]); }
      });
    });
  });
}
export const segmentedValue = (root, label) => $(`[role="radiogroup"][data-seg="${label}"] [aria-checked="true"]`, root)?.getAttribute('data-value');

// ---------------------------------------------------------------------------
// Вход
// ---------------------------------------------------------------------------

export function viewLogin({ notice = '', initialError = '', email = '' } = {}) {
  const loginIdle = 'Войти';
  authShell(`<form id="login-form" class="auth-form" novalidate>
      <div class="auth-top">
        <div class="auth-brand"><h1 class="auth-title">botstead</h1><span class="t-footnote">Вход на сервер ${esc(location.host)}</span></div>
        ${notice ? `<div class="banner banner-info" role="status"><span class="banner-icon">${ICONS.check}</span><span class="banner-text"><span class="banner-title">${esc(notice)}</span></span></div>` : ''}
        ${field({ id: 'login-email', label: 'Email', type: 'email', value: email, autocomplete: 'username', inputmode: 'email' })}
        ${field({ id: 'login-password', label: 'Пароль', type: 'password', autocomplete: 'current-password', toggle: true })}
        <div class="form-alert" id="login-alert"></div>
      </div>
      <div class="auth-bottom">
        <button type="submit" class="btn btn-primary btn-block" id="login-submit">${loginIdle}</button>
        <span class="t-footnote auth-note">Регистрации нет. Доступ выдаёт администратор по инвайту.</span>
        <button type="button" class="link-btn" id="token-link">Войти по токену владельца</button>
        <button type="button" class="link-btn" id="lang-link" data-i18n-skip>${getLang() === 'ru' ? 'English' : 'Русский'}</button>
      </div>
    </form>`);
  const form = $('#login-form');
  const emailEl = $('#login-email');
  const passEl = $('#login-password');
  const submit = $('#login-submit');
  const alertBox = $('#login-alert');
  wireToggles(form);
  $('#token-link').addEventListener('click', () => viewTokenGate());
  $('#lang-link').addEventListener('click', () => setLang(getLang() === 'ru' ? 'en' : 'ru'));

  const showServerState = (kind) => {
    if (kind === 'down') setAlert(alertBox, 'Сервер не отвечает', 'Данные не отправлены. Проверьте сеть или VPN.');
    else setAlert(alertBox, 'Сервер не отвечает', 'Попробуйте ещё раз через минуту.');
    submit.innerHTML = `${ICONS.retry}Повторить`;
  };
  if (initialError) showServerState(initialError === 'down' ? 'down' : 'server');

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    clearErrors(form);
    const emailValue = emailEl.value.trim();
    if (!emailValue) { setFieldError(emailEl, 'Введите email'); emailEl.focus(); return; }
    if (!passEl.value) { setFieldError(passEl, 'Введите пароль'); passEl.focus(); return; }
    lockForm(form, true);
    setBusy(submit, true, 'Вхожу', loginIdle);
    try {
      const user = await api.login(emailValue, passEl.value);
      ctx.signedIn(user);
      ctx.afterLogin();
    } catch (err) {
      lockForm(form, false);
      setBusy(submit, false, '', loginIdle);
      if (err.status === 401) {
        setFieldError(passEl, 'Email или пароль не подходят');
        passEl.select();
      } else if (err.status === 429) {
        setAlert(alertBox, 'Слишком много попыток', 'Подождите минуту и повторите.');
      } else {
        showServerState(isNetworkError(err) ? 'down' : 'server');
      }
    }
  });
}

// Нет связи с сервером: оболочка открылась из кэша, данные не показываем.
export function viewOffline() {
  authShell(`<div class="auth-top"><h1 class="auth-title small">botstead</h1>${stateHtml({ iconHtml: ICONS.cloudOff, title: 'Нет связи', text: 'Сервер недоступен. Данные появятся, когда связь вернётся.', actions: retryButton, kind: 'state-error' })}</div>`);
  const button = $('[data-act="retry"]');
  button.addEventListener('click', () => ctx.rerender());
  button.focus();
}

// Устаревший вход: OWNER_TOKEN в localStorage. Показывается только по ссылке с экрана входа.
export function viewTokenGate() {
  authShell(`<form id="token-form" class="auth-form" novalidate>
      <div class="auth-top">
        <div class="auth-brand"><h1 class="auth-title">botstead</h1><span class="t-footnote">Вход по токену владельца. Это устаревший способ: лучше вход по паролю.</span></div>
        ${field({ id: 'token-input', label: 'Токен владельца', type: 'password', autocomplete: 'off', toggle: true })}
        <div class="form-alert" id="token-alert"></div>
      </div>
      <div class="auth-bottom">
        <button type="submit" class="btn btn-primary btn-block" id="token-submit">Продолжить</button>
        <button type="button" class="link-btn" id="token-back">Войти по паролю</button>
      </div>
    </form>`);
  const form = $('#token-form');
  const input = $('#token-input');
  const submit = $('#token-submit');
  wireToggles(form);
  $('#token-back').addEventListener('click', () => ctx.rerender());
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    clearErrors(form);
    const value = input.value.trim();
    if (!value) { setFieldError(input, 'Введите токен'); input.focus(); return; }
    setBusy(submit, true, 'Проверяю', 'Продолжить');
    api.setToken(value);
    try {
      const me = await api.authMe();
      ctx.signedIn(me);
      ctx.afterLogin();
    } catch (err) {
      api.setToken('');
      setBusy(submit, false, '', 'Продолжить');
      if (err.status === 401) setFieldError(input, 'Токен не подходит');
      else failureAlert($('#token-alert'), err);
    }
  });
}

// ---------------------------------------------------------------------------
// Первичная настройка: создание первого администратора
// ---------------------------------------------------------------------------

export function viewSetup() {
  const idle = 'Создать администратора';
  authShell(`<form id="setup-form" class="auth-form" novalidate>
      <div class="auth-top">
        <div class="auth-brand"><h1 class="auth-title">Первая настройка</h1><span class="t-footnote">На сервере ${esc(location.host)} ещё нет пользователей. Создайте администратора.</span></div>
        ${field({ id: 'setup-email', label: 'Email', type: 'email', autocomplete: 'username', inputmode: 'email', hint: 'Для входа на сервер' })}
        ${field({ id: 'setup-password', label: 'Пароль', type: 'password', autocomplete: 'new-password', toggle: true, hint: 'От 10 символов' })}
        ${field({ id: 'setup-repeat', label: 'Пароль ещё раз', type: 'password', autocomplete: 'new-password' })}
        ${field({ id: 'setup-code', label: 'Одноразовый код', type: 'text', mono: true, hint: 'Код есть в логе ядра. На сервере выполните:', describedBy: 'setup-cmd' })}
        <div class="cmd-block" id="setup-cmd"><code class="cmd-code" id="setup-cmd-text">docker logs bothub-core 2&gt;&amp;1 | grep setup_code</code><button type="button" class="icon-btn" id="setup-cmd-copy" aria-label="Скопировать команду">${ICONS.copy}</button><span class="sr-only" id="setup-cmd-status" role="status"></span></div>
        <div class="form-alert" id="setup-alert"></div>
      </div>
      <div class="auth-bottom">
        <button type="submit" class="btn btn-primary btn-block" id="setup-submit">${idle}</button>
        <button type="button" class="link-btn" id="setup-login">Уже есть аккаунт: войти</button>
      </div>
    </form>`);
  const form = $('#setup-form');
  const el = { email: $('#setup-email'), password: $('#setup-password'), repeat: $('#setup-repeat'), code: $('#setup-code') };
  const submit = $('#setup-submit');
  const alertBox = $('#setup-alert');
  wireToggles(form);
  $('#setup-cmd-copy').addEventListener('click', async (e) => {
    const button = e.currentTarget;
    const ok = await copyText($('#setup-cmd-text').textContent, $('#setup-cmd-text'));
    $('#setup-cmd-status').textContent = ok ? 'Команда скопирована' : 'Не удалось скопировать, выделите команду вручную';
    if (ok) {
      button.innerHTML = ICONS.check;
      setTimeout(() => { button.innerHTML = ICONS.copy; }, 2000);
    }
  });
  $('#setup-login').addEventListener('click', () => { history.replaceState(null, '', `${location.pathname}${location.search}#/`); ctx.rerender(); });

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    clearErrors(form);
    const email = el.email.value.trim().toLowerCase();
    if (!email.includes('@')) { setFieldError(el.email, 'Введите email'); el.email.focus(); return; }
    if (el.password.value.length < 10) { setFieldError(el.password, 'Пароль короче 10 символов'); el.password.focus(); return; }
    if (el.repeat.value !== el.password.value) { setFieldError(el.repeat, 'Пароли не совпадают'); el.repeat.focus(); return; }
    if (!el.code.value.trim()) { setFieldError(el.code, 'Введите код из лога ядра'); el.code.focus(); return; }
    lockForm(form, true);
    setBusy(submit, true, 'Создаю', idle);
    try {
      await api.setup(email, el.password.value, el.code.value.trim());
    } catch (err) {
      lockForm(form, false);
      setBusy(submit, false, '', idle);
      if (err.status === 401) setFieldError(el.code, 'Код не подходит');
      else if (err.status === 409) setAlert(alertBox, 'Сервер уже настроен', 'Пользователи есть. Войдите под своим аккаунтом.');
      else if (err.status === 400 || err.status === 422) setAlert(alertBox, 'Проверьте email и пароль', 'Пароль от 10 символов, email с «@».');
      else failureAlert(alertBox, err, 'Администратор не создан. ');
      return;
    }
    try {
      const user = await api.login(email, el.password.value);
      ctx.signedIn(user);
      markWelcome();
      ctx.afterLogin('#/welcome');
    } catch {
      ctx.showLogin({ notice: 'Администратор создан. Войдите с этим паролем.', email });
    }
  });
}

// ---------------------------------------------------------------------------
// Принятие инвайта
// ---------------------------------------------------------------------------

const GONE = {
  invalid: { title: 'Инвайт не работает', text: 'Ссылка истекла, уже использована или отозвана. Новую выдаёт администратор сервера.' },
  expired: { title: 'Инвайт истёк', text: 'Ссылка больше не работает. Новую выдаёт администратор сервера.' },
  used: { title: 'Инвайт уже использован', text: 'Ссылка одноразовая: аккаунт по ней уже создан.' },
};

function inviteGone(kind) {
  const { title, text } = GONE[kind];
  authShell(`<div class="auth-top">${stateHtml({ iconHtml: ICONS.link, title, text, actions: '<button type="button" class="btn btn-secondary" data-nav="#/">Уже есть аккаунт: войти</button>' })}</div>`);
}

export async function viewInvite(token) {
  authShell(`<div class="auth-top"><h1 class="auth-title small">Приглашение</h1>${loadingHtml('Проверяю приглашение')}</div>`);
  let valid = false;
  try {
    valid = (await api.checkInvite(token)).valid === true;
  } catch (err) {
    authShell(`<div class="auth-top"><h1 class="auth-title small">Приглашение</h1>${stateHtml({ iconHtml: ICONS.cloudOff, title: 'Не получилось проверить приглашение', text: `${failure(err).title}. ${failure(err).text}`, actions: retryButton, kind: 'state-error' })}</div>`);
    $('[data-act="retry"]').addEventListener('click', () => viewInvite(token));
    return;
  }
  if (!valid) { inviteGone('invalid'); return; }

  const idle = 'Создать аккаунт';
  authShell(`<form id="invite-form" class="auth-form" novalidate>
      <div class="auth-top">
        <div class="auth-brand"><h1 class="auth-title small">Приглашение</h1><span class="t-footnote">Вас приглашают на сервер ${esc(location.host)}. Создайте аккаунт, чтобы войти.</span></div>
        ${field({ id: 'invite-email', label: 'Email', type: 'email', autocomplete: 'username', inputmode: 'email', hint: 'Для входа на сервер' })}
        ${field({ id: 'invite-password', label: 'Пароль', type: 'password', autocomplete: 'new-password', toggle: true, hint: 'От 10 символов' })}
        ${field({ id: 'invite-repeat', label: 'Пароль ещё раз', type: 'password', autocomplete: 'new-password' })}
        <div class="form-alert" id="invite-alert"></div>
      </div>
      <div class="auth-bottom">
        <button type="submit" class="btn btn-primary btn-block" id="invite-submit">${idle}</button>
        <span class="t-footnote auth-note">Боты, модели и память у каждого пользователя свои.</span>
      </div>
    </form>`);
  const form = $('#invite-form');
  const el = { email: $('#invite-email'), password: $('#invite-password'), repeat: $('#invite-repeat') };
  const submit = $('#invite-submit');
  const alertBox = $('#invite-alert');
  wireToggles(form);

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    clearErrors(form);
    const email = el.email.value.trim().toLowerCase();
    if (!email.includes('@')) { setFieldError(el.email, 'Введите email'); el.email.focus(); return; }
    if (el.password.value.length < 10) { setFieldError(el.password, 'Пароль короче 10 символов'); el.password.focus(); return; }
    if (el.repeat.value !== el.password.value) { setFieldError(el.repeat, 'Пароли не совпадают'); el.repeat.focus(); return; }
    lockForm(form, true);
    setBusy(submit, true, 'Создаю аккаунт', idle);
    try {
      await api.acceptInvite(token, email, el.password.value);
    } catch (err) {
      if (err.status === 410) {
        inviteGone(err.code === 'invite_expired' ? 'expired' : err.code === 'invite_used' ? 'used' : 'invalid');
        return;
      }
      lockForm(form, false);
      setBusy(submit, false, '', idle);
      if (err.status === 409) setFieldError(el.email, 'Этот email уже занят');
      else if (err.status === 400 || err.status === 422) setAlert(alertBox, 'Проверьте email и пароль', 'Пароль от 10 символов, email с «@».');
      else failureAlert(alertBox, err, 'Аккаунт не создан. ');
      return;
    }
    try {
      const user = await api.login(email, el.password.value);
      ctx.signedIn(user);
      markWelcome();
      ctx.afterLogin('#/welcome');
    } catch {
      ctx.showLogin({ notice: 'Аккаунт создан. Войдите с этим паролем.', email });
    }
  });
}

// ---------------------------------------------------------------------------
// Первый вход: что делать дальше
// ---------------------------------------------------------------------------

export async function viewWelcome() {
  const user = ctx.session.user;
  // Модели у каждого свои: шаг «Создать бота» открыт, когда есть хотя бы одна включённая модель рабочего провайдера.
  // Если проверить не удалось, это не «моделей нет»: говорим честно и даём повторить.
  let ready = 0;
  let failed = false;
  try {
    const [providers, models] = await Promise.all([api.listProviders(), api.listModels()]);
    ready = usableCount(providers, models);
  } catch { ready = 0; failed = true; }
  const step = (n, title, text, { href = '', off = false } = {}) => {
    const inner = `<span class="step-num" aria-hidden="true">${n}</span><span class="stack gap-1 flex-1 min-w-0"><span class="t-callout">${esc(title)}</span><span class="t-footnote">${esc(text)}</span></span>`;
    const card = href ? `<a class="step-card" href="${href}">${inner}<span class="row-chevron" aria-hidden="true">${ICONS.chevronRight}</span></a>` : `<div class="step-card${off ? ' is-off' : ''}"${off ? ' aria-disabled="true"' : ''}>${inner}</div>`;
    return `<li>${card}</li>`;
  };
  const modelText = failed
    ? 'Не удалось проверить модели. Проверьте сеть или VPN и повторите.'
    : ready
      ? `Подключено моделей: ${ready}. Добавить ещё можно в разделе «Провайдеры».`
      : 'Каждый подключает свои модели в разделе «Провайдеры»: по API-ключу, через свой адрес или по подписке.';
  authShell(`<div class="auth-top">
      <div class="auth-brand"><h1 class="auth-title small">Аккаунт создан</h1><span class="t-footnote">${esc(user.email)} на сервере ${esc(location.host)}. Что дальше:</span></div>
      <ol class="step-list">
        ${step(1, 'Модель', modelText, { href: '#/settings/providers' })}
        ${step(2, 'Создать бота', ready ? 'Словами описать, что он должен делать' : failed ? 'Станет доступно после проверки моделей' : 'Станет доступно, когда будет хотя бы одна включённая модель', { off: !ready })}
        ${step(3, 'Добавить на экран «Домой»', 'Так приходят уведомления о решениях')}
      </ol>
    </div>
    <div class="auth-bottom">
      ${failed
        ? `<button type="button" class="btn btn-primary btn-block" data-act="retry">${ICONS.retry}Повторить проверку</button>`
        : ready
          ? `<a class="btn btn-primary btn-block" href="#/bots/new">${ICONS.plus}Создать бота</a>`
          : `<a class="btn btn-primary btn-block" href="#/settings/providers">${ICONS.plug}Подключить модель</a>`}
      <a class="btn btn-ghost btn-block" href="#/">Позже</a>
    </div>`);
  if (failed) ctx.app.querySelector('[data-act="retry"]').addEventListener('click', () => viewWelcome());
}

// ---------------------------------------------------------------------------
// Хаб настроек
// ---------------------------------------------------------------------------

const ROLE_LABEL = { admin: 'админ', member: 'участник' };
const ROLE_FULL = { admin: 'администратор', member: 'участник' };

function settingsRow({ href, iconHtml, title, text = '', soon = false, extra = '' }) {
  if (soon) {
    return `<a class="settings-row is-soon" href="${href}"><span class="row-icon" aria-hidden="true">${iconHtml}</span><span class="row-body"><span class="row-title">${esc(title)}</span></span><span class="soon-pill">Скоро</span></a>`;
  }
  return `<a class="settings-row" href="${href}"><span class="row-icon" aria-hidden="true">${iconHtml}</span><span class="row-body"><span class="row-title">${esc(title)}</span><span class="row-sub">${esc(text)}</span></span>${extra}<span class="row-chevron" aria-hidden="true">${ICONS.chevronRight}</span></a>`;
}

export async function viewSettings() {
  const user = ctx.session.user;
  const admin = user.role === 'admin';
  await ctx.frame({
    title: 'Настройки',
    subtitle: `${user.email} · ${ROLE_FULL[user.role] || user.role}`,
    backHref: '#/',
    body: `<div class="page-narrow stack gap-4">
      ${admin && user.legacy_auth ? `<div class="banner banner-attention" role="status"><span class="banner-icon">${ICONS.alert}</span><span class="banner-text"><span class="banner-title">Включён старый вход по токену</span><span class="banner-sub">После перехода на пароли его стоит отключить на сервере.</span></span></div>` : ''}
      <section class="stack gap-2" aria-labelledby="st-bots"><h2 class="section-label" id="st-bots">Боты</h2>
        <div class="settings-group">
          ${settingsRow({ href: '#/settings/providers', iconHtml: ICONS.plug, title: 'Провайдеры', text: 'Модели для ботов' })}
          ${settingsRow({ href: '#/memory', iconHtml: ICONS.memory, title: 'Память', text: 'Что боты запомнили' })}
          ${settingsRow({ href: '#/settings/permissions', iconHtml: ICONS.sliders, title: 'Разрешения', soon: true })}
          ${settingsRow({ href: '#/settings/activity', iconHtml: ICONS.activity, title: 'Активность', text: 'Журнал действий и пауза ботов' })}
        </div>
      </section>
      ${admin
        ? `<section class="stack gap-2" aria-labelledby="st-server"><h2 class="section-label" id="st-server">Сервер</h2>
        <div class="settings-group">
          ${settingsRow({ href: '#/settings/users', iconHtml: ICONS.users, title: 'Пользователи', text: 'Роли, отключение, инвайты' })}
          ${settingsRow({ href: '#/settings/provider-requests', iconHtml: ICONS.lock, title: 'Запросы на внутренние адреса', text: 'Адреса в сети, которые ждут решения', extra: '<span class="badge badge-attention" data-requests-badge hidden></span>' })}
        </div></section>`
        : '<p class="t-footnote">Пользователями и инвайтами управляет администратор сервера.</p>'}
      <section class="stack gap-2" aria-labelledby="st-lang"><h2 class="section-label" id="st-lang">Язык</h2>
        ${segmentedHtml('Язык интерфейса', LANGS.map((l) => ({ value: l.code, label: l.label })), getLang(), { skip: true })}
      </section>
      <section class="stack gap-2" aria-labelledby="st-account"><h2 class="section-label" id="st-account">Аккаунт</h2>
        <div class="settings-group">
          ${settingsRow({ href: '#/settings/password', iconHtml: ICONS.lock, title: 'Сменить пароль', text: 'После смены нужно войти снова' })}
          ${settingsRow({ href: '#/settings/sessions', iconHtml: ICONS.device, title: 'Мои сессии', text: 'Где выполнен вход' })}
        </div>
        <button type="button" class="btn btn-danger btn-block" data-act="logout">${ICONS.logout}Выйти</button>
      </section>
    </div>`,
  });
  $('[data-act="logout"]').addEventListener('click', (e) => {
    e.currentTarget.disabled = true;
    ctx.signOut();
  });
  const langGroup = $('[data-seg="Язык интерфейса"]');
  if (langGroup) {
    const box = langGroup.parentElement;
    wireSegmented(box);
    // Выбор языка сохраняется и перезагружает страницу (слушатель bothub:lang в app.js).
    const applyLang = () => {
      const value = segmentedValue(box, 'Язык интерфейса');
      if (value && value !== getLang()) setLang(value);
    };
    langGroup.addEventListener('click', applyLang);
    langGroup.addEventListener('keydown', applyLang);
  }
  if (admin) paintRequestsBadge();
}

// Счётчик запросов на внутренние адреса в строке настроек: не задерживает экран, при ошибке бейджа просто нет.
async function paintRequestsBadge() {
  let count = 0;
  try { count = (await api.listProviderRequests()).length; } catch { return; }
  const badge = $('[data-requests-badge]');
  if (!badge || !count) return;
  badge.hidden = false;
  badge.innerHTML = `${count}<span class="sr-only"> ${plural(count, 'запрос ждёт', 'запроса ждут', 'запросов ждут')} решения</span>`;
}

// ---------------------------------------------------------------------------
// Заглушка «Раздел в разработке»: один экран на три раздела, внутри то, что можно делать уже сейчас.
// ---------------------------------------------------------------------------

const STUBS = {
  permissions: {
    title: 'Разрешения',
    icon: ICONS.sliders,
    text: 'Режимы инструментов для каждого бота (Без вопроса, Спросить, По команде, Запрещено) появятся позже.',
    now: [{ href: '#/approvals', label: 'Решения', hint: 'Подтверждения приходят сюда' }],
  },
};

export async function viewStub(kind) {
  const stub = STUBS[kind];
  const now = stub.now.map((item) => `<li><a class="settings-row" href="${item.href}"><span class="row-body"><span class="row-title">${esc(item.label)}</span><span class="row-sub wrap">${esc(item.hint)}</span></span><span class="row-chevron" aria-hidden="true">${ICONS.chevronRight}</span></a></li>`).join('');
  await ctx.frame({
    title: stub.title,
    subtitle: 'Настройки',
    backHref: '#/settings',
    body: `<div class="page-narrow stack gap-4">
      ${stateHtml({ iconHtml: stub.icon, title: 'Раздел в разработке', text: stub.text })}
      <section class="stack gap-2" aria-labelledby="stub-now"><h2 class="section-label" id="stub-now">Что можно сейчас</h2><ul class="settings-group list-plain">${now}</ul></section>
      <a class="btn btn-secondary" href="#/settings">К настройкам</a>
    </div>`,
  });
}

// ---------------------------------------------------------------------------
// Смена пароля
// ---------------------------------------------------------------------------

export async function viewPassword() {
  const idle = 'Сменить пароль';
  await ctx.frame({
    title: 'Смена пароля',
    subtitle: 'Настройки',
    backHref: '#/settings',
    body: `<div class="page-narrow" id="pw-content"><form id="pw-form" class="stack gap-4 card card-pad" novalidate>
        ${field({ id: 'pw-old', label: 'Текущий пароль', type: 'password', autocomplete: 'current-password', toggle: true })}
        ${field({ id: 'pw-new', label: 'Новый пароль', type: 'password', autocomplete: 'new-password', hint: 'От 10 символов' })}
        ${field({ id: 'pw-repeat', label: 'Новый пароль ещё раз', type: 'password', autocomplete: 'new-password' })}
        <div class="form-alert" id="pw-alert"></div>
        <p class="t-footnote">После смены все сессии закроются, потребуется войти снова.</p>
        <button type="submit" class="btn btn-primary btn-block desktop-only" form="pw-form" id="pw-submit-inline">${idle}</button>
      </form></div>`,
    mobileActions: `<button type="submit" class="btn btn-primary btn-block" form="pw-form" id="pw-submit">${idle}</button>`,
  });
  const form = $('#pw-form');
  const el = { old: $('#pw-old'), next: $('#pw-new'), repeat: $('#pw-repeat') };
  const submits = Array.from(document.querySelectorAll('#pw-submit, #pw-submit-inline'));
  const alertBox = $('#pw-alert');
  wireToggles(form);
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    clearErrors(form);
    if (!el.old.value) { setFieldError(el.old, 'Введите текущий пароль'); el.old.focus(); return; }
    if (el.next.value.length < 10) { setFieldError(el.next, 'Пароль короче 10 символов'); el.next.focus(); return; }
    if (el.repeat.value !== el.next.value) { setFieldError(el.repeat, 'Пароли не совпадают'); el.repeat.focus(); return; }
    lockForm(form, true);
    submits.forEach((b) => setBusy(b, true, 'Меняю', idle));
    try {
      await api.changePassword(el.old.value, el.next.value);
    } catch (err) {
      lockForm(form, false);
      submits.forEach((b) => setBusy(b, false, '', idle));
      if (err.status === 401) { setFieldError(el.old, 'Текущий пароль не подходит'); el.old.select(); }
      else if (err.status === 400 || err.status === 422) { setFieldError(el.next, 'Пароль короче 10 символов'); el.next.focus(); }
      else if (err.status === 429) setAlert(alertBox, 'Слишком много попыток', 'Подождите минуту и повторите. Пароль не изменён.');
      else failureAlert(alertBox, err, 'Пароль не изменён. ');
      return;
    }
    const email = ctx.session.user.email;
    await ctx.endSession();
    $('#pw-content').innerHTML = stateHtml({ iconHtml: ICONS.check, title: 'Пароль изменён', text: 'Все сессии закрыты. Войдите с новым паролем.', actions: '<button type="button" class="btn btn-primary" data-act="to-login">Войти</button>' });
    document.querySelector('.action-bar')?.remove();
    $('[data-act="to-login"]').addEventListener('click', () => ctx.showLogin({ notice: 'Пароль изменён. Войдите с новым паролем.', email }));
  });
}

// ---------------------------------------------------------------------------
// Мои сессии
// ---------------------------------------------------------------------------

function deviceLabel(ua = '') {
  const os = /iPhone/.test(ua) ? 'iPhone' : /iPad/.test(ua) ? 'iPad' : /Android/.test(ua) ? 'Android'
    : /Macintosh|Mac OS X/.test(ua) ? 'Mac' : /Windows/.test(ua) ? 'Windows' : /Linux/.test(ua) ? 'Linux' : '';
  const browser = /Edg\//.test(ua) ? 'Edge' : /Firefox\//.test(ua) ? 'Firefox' : /Chrome\/|CriOS/.test(ua) ? 'Chrome' : /Safari\//.test(ua) ? 'Safari' : '';
  return [os, browser].filter(Boolean).join(' · ') || 'Неизвестное устройство';
}

// Тип устройства по user_agent: телефон, планшет, компьютер; без понятного user_agent просто браузер.
const DEVICE_ICON = { phone: ICONS.device, tablet: ICONS.tablet, desktop: ICONS.laptop, other: ICONS.browser };
function deviceKind(ua = '') {
  if (/iPhone|Android.+Mobile|Mobile.+Android/.test(ua)) return 'phone';
  if (/iPad|Android/.test(ua)) return 'tablet';
  if (/Macintosh|Mac OS X|Windows|Linux|X11/.test(ua)) return 'desktop';
  return 'other';
}

// Единственное место, где решается, какая сессия текущая.
// 1) Когда API начнёт отдавать поле current в строках /sessions, оно главное.
// 2) Пока поля нет, сравниваем user_agent с navigator.userAgent: помечаем, только если совпала ровно одна сессия.
// 3) Иначе (ничего или несколько совпадений) не помечаем никого.
export function currentSessionId(rows) {
  if (rows.some((s) => typeof s.current === 'boolean')) return rows.find((s) => s.current === true)?.id_hash ?? null;
  const same = rows.filter((s) => s.user_agent && s.user_agent === navigator.userAgent);
  return same.length === 1 ? same[0].id_hash : null;
}

export async function viewSessions() {
  await ctx.frame({
    title: 'Мои сессии',
    subtitle: 'Настройки',
    backHref: '#/settings',
    body: '<div class="page-narrow stack gap-3" id="sessions-content" aria-busy="true"></div>',
  });
  const box = $('#sessions-content');
  let currentId = null;

  async function endSession(id) {
    try {
      await api.deleteSession(id);
    } catch (err) {
      if (err.status !== 404) throw err; // уже закрыта: для списка всё равно что успех
    }
  }

  box.addEventListener('click', (e) => {
    const retry = e.target.closest('[data-act="retry"]');
    if (retry) { load(); return; }
    const revoke = e.target.closest('[data-revoke]');
    if (!revoke) return;
    const id = revoke.getAttribute('data-revoke');
    const name = revoke.getAttribute('data-name');
    const current = id === currentId;
    confirmAction({
      title: current ? 'Выйти на этом устройстве?' : 'Завершить сессию?',
      subtitle: name,
      text: current
        ? 'Сессия закроется, на этом устройстве потребуется войти снова.'
        : 'Это устройство потеряет доступ к аккаунту, там потребуется войти снова.',
      confirmLabel: 'Завершить',
      run: () => endSession(id),
      describeError: (err) => failure(err, 'Сессия не закрыта. '),
      done: load,
    });
  });

  async function load() {
    box.setAttribute('aria-busy', 'true');
    box.innerHTML = loadingHtml('Загружаю сессии');
    let rows;
    try {
      rows = (await api.listSessions()).filter((s) => !s.revoked_at);
    } catch (err) {
      if (err.status === 401) return; // ctx покажет вход
      box.removeAttribute('aria-busy');
      box.innerHTML = stateHtml({ iconHtml: ICONS.cloudOff, title: 'Сессии не загрузились', text: `${failure(err).title}. ${failure(err).text}`, actions: retryButton, kind: 'state-error' });
      return;
    }
    box.removeAttribute('aria-busy');
    if (!rows.length) {
      box.innerHTML = stateHtml({ iconHtml: ICONS.device, title: 'Активных сессий нет', text: 'Войдите снова, чтобы продолжить работу.' });
      return;
    }
    currentId = currentSessionId(rows);
    box.innerHTML = `<div class="form-alert" id="sessions-alert"></div>
      <h2 class="section-label">Активные · ${rows.length}</h2>
      <ul class="list-plain">${rows.map((s) => {
        const name = deviceLabel(s.user_agent);
        const here = s.id_hash === currentId;
        return `<li class="list-row"><span class="row-icon" aria-hidden="true" data-device="${deviceKind(s.user_agent)}">${DEVICE_ICON[deviceKind(s.user_agent)]}</span><span class="row-body"><span class="title-line"><span class="row-title wrap">${esc(name)}</span>${here ? '<span class="badge badge-sunken">Это устройство</span>' : ''}</span><span class="row-sub wrap">Вход ${esc(fmtDateTime(s.created_at))}</span><span class="row-sub wrap">Действует до ${esc(fmtDate(s.expires_at))}</span></span><button type="button" class="btn btn-secondary" data-revoke="${esc(s.id_hash)}" data-name="${esc(name)}" aria-label="Завершить сессию: ${esc(name)}">Завершить</button></li>`;
      }).join('')}</ul>
      <p class="t-footnote">Завершение текущей сессии выйдет из аккаунта на этом устройстве.</p>`;
  }
  await load();
}

// ---------------------------------------------------------------------------
// Пользователи и инвайты (только админ)
// ---------------------------------------------------------------------------

function readNotes() {
  try { return JSON.parse(localStorage.getItem(NOTES_KEY) || '{}'); } catch { return {}; }
}
function saveNote(id, note) {
  if (!note) return;
  try { localStorage.setItem(NOTES_KEY, JSON.stringify({ ...readNotes(), [id]: note })); } catch { /* память недоступна */ }
}

// Строки срока инвайта: вторая строка заголовка с датой окончания целиком, ниже сколько осталось.
function expiryLines(iso) {
  const ms = new Date(iso).getTime() - Date.now();
  if (ms <= 0) return [`Истёк ${fmtDateTime(iso)}`];
  const hours = Math.round(ms / 3600000);
  const left = hours < 48 ? `${Math.max(1, hours)} ч` : `${Math.round(hours / 24)} дн`;
  return [`Действует до ${fmtDateTime(iso)}`, `Осталось ${left}`];
}

const inviteLabel = (note) => (note ? `Инвайт: ${note}` : 'Инвайт без заметки');
export const isDesktop = () => window.matchMedia('(min-width: 1024px)').matches;

function userPatchError(err) {
  if (err.status === 409) return { title: 'Нельзя изменить последнего администратора', text: 'Сначала назначьте другого администратора.' };
  if (err.status === 403) return { title: 'Нужны права администратора', text: 'Изменения не сохранены.' };
  if (err.status === 404) return { title: 'Пользователь не найден', text: 'Обновите список.' };
  return failure(err, 'Изменения не сохранены. ');
}

export function forbiddenState(text = 'Пользователями и инвайтами управляет администратор сервера.') {
  return stateHtml({ iconHtml: ICONS.lock, title: 'Раздел только для администратора', text, actions: '<a class="btn btn-secondary" href="#/settings">К настройкам</a>' });
}

export async function viewUsers() {
  const me = ctx.session.user;
  if (me.role !== 'admin') {
    await ctx.frame({ title: 'Пользователи', subtitle: `Сервер ${location.host}`, backHref: '#/settings', body: `<div class="page-narrow">${forbiddenState()}</div>` });
    return;
  }
  await ctx.frame({
    title: 'Пользователи',
    subtitle: `Сервер ${location.host}`,
    backHref: '#/settings',
    body: '<div class="page-wide stack gap-4"><div class="form-alert" id="users-alert"></div><div class="stack gap-4" id="users-content" aria-busy="true"></div></div>',
    mobileActions: `<button type="button" class="btn btn-primary btn-block" data-act="new-invite">${ICONS.plus}Создать инвайт</button>`,
    desktopAside: '<div class="stack gap-3" id="users-aside"></div>',
  });
  const content = $('#users-content');
  const aside = $('#users-aside');
  let users = [];
  let invites = [];

  const userRow = (u) => {
    const disabled = u.status === 'disabled';
    const self = u.id === me.id;
    return `<li class="list-row">
      <span class="avatar-letter" aria-hidden="true">${esc((u.email[0] || '?').toUpperCase())}</span>
      <span class="row-body"><span class="row-title">${esc(u.email)}${self ? ' · это вы' : ''}</span><span class="row-sub status-line">${esc(ROLE_LABEL[u.role] || u.role)} · <span class="status-dot ${disabled ? 'neutral' : 'success'}"></span>${disabled ? 'Отключён' : 'Активен'}</span></span>
      <button type="button" class="icon-btn" data-user="${esc(u.id)}" aria-label="Действия: ${esc(u.email)}">${ICONS.more}</button>
    </li>`;
  };
  const inviteRow = (inv, notes) => {
    const expired = new Date(inv.expires_at).getTime() <= Date.now();
    const note = notes[inv.token_hash] || '';
    const title = inviteLabel(note);
    const roleBadge = `<span class="badge ${inv.role === 'admin' ? 'badge-attention' : 'badge-sunken'}">${esc(ROLE_LABEL[inv.role] || inv.role)}</span>`;
    return `<li class="list-row invite-row">
      <span class="row-icon" aria-hidden="true">${ICONS.link}</span>
      <span class="row-body"><span class="title-line"><span class="row-title wrap">${esc(title)}</span>${roleBadge}</span>${expiryLines(inv.expires_at).map((line) => `<span class="row-sub wrap">${esc(line)}</span>`).join('')}</span>
      <span class="row-actions">${expired
        ? `<button type="button" class="btn btn-ghost" data-reissue="${esc(inv.role)}" aria-label="Выдать заново: ${esc(note || 'без заметки')}">Заново</button><button type="button" class="icon-btn" data-revoke="${esc(inv.token_hash)}" data-expired="1" aria-label="Убрать инвайт: ${esc(note || 'без заметки')}">${ICONS.close}</button>`
        : `<button type="button" class="btn btn-secondary" data-revoke="${esc(inv.token_hash)}" data-label="${esc(title)}" aria-label="Отозвать инвайт: ${esc(note || 'без заметки')}">Отозвать</button>`}</span>
    </li>`;
  };

  function paint() {
    const notes = readNotes();
    const visible = invites.filter((inv) => !inv.used_at);
    const usersBlock = `<section class="stack gap-2" aria-labelledby="users-h"><h2 class="section-label" id="users-h">Пользователи · ${users.length}</h2><ul class="list-plain">${users.map(userRow).join('')}</ul>
      <p class="t-footnote">Отключённый пользователь не может войти, его боты остановлены, данные сохранены.</p></section>`;
    const emptyBlock = users.length <= 1 && !visible.length
      ? stateHtml({ iconHtml: ICONS.users, title: 'Пока здесь только администратор', text: 'Инвайт даёт одноразовую ссылку со сроком. Регистрации без ссылки нет.' }) : '';
    const invitesBlock = `<section class="stack gap-2" aria-labelledby="invites-h"><h2 class="section-label" id="invites-h">Инвайты · ${visible.length}</h2>
      ${visible.length ? `<ul class="list-plain">${visible.map((inv) => inviteRow(inv, notes)).join('')}</ul>` : '<p class="t-footnote">Активных инвайтов нет.</p>'}
      <p class="t-footnote">Инвайт одноразовый. Приглашённый видит только своих ботов и свои модели.</p></section>`;
    content.removeAttribute('aria-busy');
    if (aside) {
      content.innerHTML = usersBlock + emptyBlock;
      aside.innerHTML = `${invitesBlock}<button type="button" class="btn btn-primary btn-block" data-act="new-invite">${ICONS.plus}Создать инвайт</button>`;
    } else {
      content.innerHTML = usersBlock + emptyBlock + invitesBlock;
    }
  }

  async function load() {
    content.setAttribute('aria-busy', 'true');
    setAlert($('#users-alert'), '');
    if (!users.length) content.innerHTML = loadingHtml('Загружаю пользователей');
    try {
      [users, invites] = await Promise.all([api.listUsers(), api.listInvites()]);
    } catch (err) {
      if (err.status === 401) return;
      content.removeAttribute('aria-busy');
      if (err.status === 403) { content.innerHTML = forbiddenState(); return; }
      content.innerHTML = stateHtml({ iconHtml: ICONS.cloudOff, title: 'Список не загрузился', text: `${failure(err).title}. Пользователи и инвайты не изменены. ${failure(err).text}`, actions: retryButton, kind: 'state-error' });
      return;
    }
    paint();
  }

  async function handleClick(e) {
    if (e.target.closest('[data-act="retry"]')) { load(); return; }
    if (e.target.closest('[data-act="new-invite"]')) { openInviteDialog(); return; }
    const reissue = e.target.closest('[data-reissue]');
    if (reissue) { openInviteDialog(reissue.getAttribute('data-reissue')); return; }
    const userButton = e.target.closest('[data-user]');
    if (userButton) { openUserDialog(users.find((u) => u.id === userButton.getAttribute('data-user'))); return; }
    const revoke = e.target.closest('[data-revoke]');
    if (!revoke) return;
    const id = revoke.getAttribute('data-revoke');
    const dropInvite = async () => {
      try {
        await api.deleteInvite(id);
      } catch (err) {
        if (err.status !== 404) throw err; // уже убран: список просто обновится
      }
    };
    if (revoke.hasAttribute('data-expired')) {
      // Истёкший инвайт и так не работает: убираем из списка без вопросов.
      revoke.disabled = true;
      try {
        await dropInvite();
      } catch (err) {
        revoke.disabled = false;
        const { title, text } = userPatchError(err);
        setAlert($('#users-alert'), title, text);
        return;
      }
      load();
      return;
    }
    confirmAction({
      title: 'Отозвать инвайт?',
      subtitle: revoke.getAttribute('data-label'),
      text: 'Ссылка перестанет работать: по ней нельзя будет создать аккаунт, даже если её уже отправили.',
      confirmLabel: 'Отозвать',
      run: dropInvite,
      done: load,
    });
  }
  content.addEventListener('click', handleClick);
  aside?.addEventListener('click', handleClick);
  $('.action-bar [data-act="new-invite"]')?.addEventListener('click', () => openInviteDialog());

  function openUserDialog(user) {
    if (!user) return;
    const disabled = user.status === 'disabled';
    const subtitle = `${ROLE_LABEL[user.role] || user.role} · ${disabled ? 'отключён' : 'активен'}`;
    const mainBody = `<div class="form-alert" id="ua-alert"></div>
        <div class="stack gap-2">
          <button type="button" class="btn btn-secondary btn-block" data-do="role">${user.role === 'admin' ? 'Сделать участником' : 'Сделать админом'}</button>
          <button type="button" class="btn ${disabled ? 'btn-secondary' : 'btn-danger'} btn-block" data-do="status">${disabled ? 'Включить' : 'Отключить'}</button>
        </div>
        ${disabled ? '' : '<p class="t-footnote">Отключённый пользователь не может войти, его боты остановлены, данные сохранены.</p>'}`;
    const dlg = openDialog({ title: user.email, subtitle, content: mainBody });

    // Отключение требует подтверждения: шаг внутри того же диалога, «Отмена» возвращает к действиям.
    function askDisable() {
      dlg.setHeader('Отключить пользователя?', user.email);
      dlg.setBody(confirmBody({ text: 'Пользователь не сможет войти, его боты остановятся, данные сохранятся.', confirmLabel: 'Отключить', cancelAttr: 'data-cancel' }));
      $('[data-cancel]', dlg.el).addEventListener('click', () => { dlg.setHeader(user.email, subtitle); dlg.setBody(mainBody); });
      wireConfirm(dlg, { run: () => api.patchUser(user.id, { disabled: true }), describeError: userPatchError, done: load });
    }

    dlg.body.addEventListener('click', async (e) => {
      const action = e.target.closest('[data-do]');
      if (!action) return;
      if (action.getAttribute('data-do') === 'status' && !disabled) { askDisable(); return; }
      const patch = action.getAttribute('data-do') === 'role'
        ? { role: user.role === 'admin' ? 'member' : 'admin' }
        : { disabled: !disabled };
      dlg.body.querySelectorAll('button').forEach((b) => { b.disabled = true; });
      try {
        await api.patchUser(user.id, patch);
      } catch (err) {
        dlg.body.querySelectorAll('button').forEach((b) => { b.disabled = false; });
        const { title, text } = userPatchError(err);
        setAlert($('#ua-alert', dlg.el), title, text);
        return;
      }
      dlg.close();
      load();
    });
  }

  function openInviteDialog(presetRole = 'member') {
    const idle = 'Создать ссылку';
    // На телефоне фокус на необязательное поле открыл бы клавиатуру и закрыл половину листа: фокус на самом листе.
    const dlg = openDialog({
      title: 'Новый инвайт',
      focus: isDesktop() ? 'first' : 'dialog',
      content: `<form id="invite-new" class="stack gap-4" novalidate>
        ${field({ id: 'inv-note', label: 'Для кого', hint: 'Заметка для себя, приглашённый её не увидит. Хранится на этом устройстве.' }).replace(' aria-required="true"', '')}
        <div class="stack gap-2"><span class="t-footnote">Роль</span>${segmentedHtml('Роль', [{ value: 'member', label: 'Участник' }, { value: 'admin', label: 'Админ' }], presetRole)}</div>
        <div class="stack gap-2"><span class="t-footnote">Срок ссылки</span>${segmentedHtml('Срок ссылки', [{ value: '1', label: '1 день' }, { value: '3', label: '3 дня' }, { value: '7', label: '7 дней' }], '3')}</div>
        <div class="form-alert" id="inv-alert"></div>
        <div class="btn-row btn-row-2"><button type="button" class="btn btn-secondary" data-close>Отмена</button><button type="submit" class="btn btn-primary" id="inv-submit">${idle}</button></div>
      </form>`,
    });
    const form = $('#invite-new', dlg.el);
    wireSegmented(form);
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const role = segmentedValue(form, 'Роль');
      const days = Number(segmentedValue(form, 'Срок ссылки'));
      const note = $('#inv-note', form).value.trim();
      const submit = $('#inv-submit', form);
      clearErrors(form);
      setBusy(submit, true, 'Создаю', idle);
      let created;
      try {
        created = await api.createInvite(role, days);
      } catch (err) {
        setBusy(submit, false, '', idle);
        const { title, text } = userPatchError(err);
        setAlert($('#inv-alert', form), title, text);
        return;
      }
      saveNote(created.token_hash, note);
      load();
      showLink(dlg, created, note, role);
    });
  }

  // Ссылка живёт только в этом диалоге: после закрытия токен нигде не хранится.
  function showLink(dlg, created, note, role) {
    const link = `${location.origin}${location.pathname}#/invite/${created.token}`;
    const canShare = typeof navigator.share === 'function';
    dlg.setHeader('Ссылка готова', `${inviteLabel(note)} · ${ROLE_LABEL[role] || role}`);
    dlg.setBody(`<div class="invite-link" id="invite-link" tabindex="0">${esc(link)}</div>
      <div class="banner banner-attention" role="status"><span class="banner-icon">${ICONS.alert}</span><span class="banner-text"><span class="banner-title">Ссылка показывается один раз</span><span class="banner-sub">Одноразовая, действует до ${esc(fmtDateTime(created.expires_at))}.</span></span></div>
      <div class="form-alert" id="copy-alert"></div>
      <div class="btn-row ${canShare ? 'btn-row-2' : ''}">
        ${canShare ? `<button type="button" class="btn btn-secondary" data-do="share">${ICONS.share}Поделиться</button>` : ''}
        <button type="button" class="btn btn-primary" data-do="copy">${ICONS.copy}Скопировать</button>
      </div>
      <span class="sr-only" id="copy-status" role="status"></span>`);
    dlg.body.addEventListener('click', async (e) => {
      const action = e.target.closest('[data-do]');
      if (!action) return;
      if (action.getAttribute('data-do') === 'share') {
        try { await navigator.share({ title: 'Приглашение в botstead', url: link }); } catch { /* пользователь отменил */ }
        return;
      }
      const ok = await copyText(link, $('#invite-link', dlg.el));
      if (ok) {
        action.innerHTML = `${ICONS.check}Скопировано`;
        $('#copy-status', dlg.el).textContent = 'Ссылка скопирована';
      } else {
        setAlert($('#copy-alert', dlg.el), 'Не удалось скопировать', 'Выделите ссылку и скопируйте вручную.');
      }
    });
  }

  await load();
}

async function copyText(text, selectable) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch { /* нет доступа к буферу: пробуем выделение */ }
  try {
    const range = document.createRange();
    range.selectNodeContents(selectable);
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    return document.execCommand('copy');
  } catch { return false; }
}
