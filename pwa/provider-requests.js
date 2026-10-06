// provider-requests.js: экран администратора «Запросы на внутренние адреса» (docs/contracts.md §11).
// Участник указывает адрес во внутренней сети, провайдер встаёт в pending_admin. Администратор видит заявки
// (GET /api/admin/provider-requests) и разрешает адрес или отказывает (PATCH /api/providers/{id}/allow-private).
// Одобрение привязано к набору IP, а не к имени: в запрос уходят ровно те base_url и IP, что показаны в листе подтверждения.
import * as api from './api.js';
import { ICONS, esc, fmtAgo } from './ui.js';
import {
  context, setAlert, failure, loadingHtml, stateHtml, retryButton, openDialog, wireConfirm, confirmAction, forbiddenState,
} from './account.js';

const $ = (selector, root = document) => root.querySelector(selector);

const ipList = (ips, attr = '') => `<ul class="ip-list" aria-label="IP-адреса"${attr}>${ips.map((ip) => `<li class="t-log" data-i18n-skip>${esc(ip)}</li>`).join('')}</ul>`;

function requestRow(r, rejected) {
  const ips = Array.isArray(r.resolved_ips) ? r.resolved_ips : [];
  const approved = Array.isArray(r.approved_ips) ? r.approved_ips : [];
  const canAllow = ips.length > 0 && !r.resolve_error;
  const reapproval = !!r.reapproval;
  const who = `${r.email} · ${fmtAgo(r.created_at)}`;
  const hintId = `pr-hint-${esc(r.id)}`;
  return `<li class="card card-pad stack gap-3" data-request="${esc(r.id)}" data-reapproval="${reapproval}">
    <span class="stack gap-1 min-w-0">
      <span class="title-line"><span class="t-callout wrap" data-i18n-skip>${esc(r.name)}</span>${reapproval ? '<span class="badge badge-attention">Повторное одобрение</span>' : ''}</span>
      <span class="t-footnote wrap" data-i18n-skip>${esc(who)}</span>
    </span>
    ${reapproval ? '<p class="t-footnote">Адрес уже разрешали, но имя сервера теперь указывает на другие IP-адреса. Пока вы не разрешите их заново, провайдер не работает.</p>' : ''}
    <dl class="compare-list">
      <dt>Адрес</dt><dd class="t-log wrap" data-i18n-skip>${esc(r.base_url)}</dd>
      <dt>Сейчас указывает на</dt><dd>${ips.length ? ipList(ips) : `<span class="t-footnote" id="${hintId}">Имя сервера сейчас не разрешается в адрес, разрешать нечего. Повторите позже.</span>`}</dd>
      ${approved.length ? `<dt>Было разрешено</dt><dd>${ipList(approved)}</dd>` : ''}
    </dl>
    ${rejected ? '<p class="t-footnote" data-rejected>Вы не разрешили этот адрес. Провайдер остаётся в ожидании: участник видит это в своих настройках.</p>' : ''}
    <div class="btn-row ${rejected ? '' : 'btn-row-2'}">
      <button type="button" class="btn btn-primary" data-allow="${esc(r.id)}" aria-label="${reapproval ? 'Разрешить заново' : 'Разрешить'}: ${esc(r.name)}"${canAllow ? '' : ` disabled aria-describedby="${hintId}"`}>${reapproval ? 'Разрешить заново' : 'Разрешить'}</button>
      ${rejected ? '' : `<button type="button" class="btn btn-secondary" data-reject="${esc(r.id)}" aria-label="${reapproval ? 'Отозвать разрешение' : 'Отклонить'}: ${esc(r.name)}">${reapproval ? 'Отозвать разрешение' : 'Отклонить'}</button>`}
    </div>
  </li>`;
}

// Что сказать об ответе ядра после одобрения: статус выставляет одна проверка сразу после записи набора (§11).
function approvedNote(name, status) {
  if (status === 'ok') return { title: `Разрешено: ${name}`, text: 'Провайдер проверен и работает.' };
  if (status === 'error') return { title: `Разрешено: ${name}`, text: 'Проверка после одобрения не прошла: участнику нужно исправить ключ или адрес.' };
  return { title: `Разрешено: ${name}`, text: 'Проверка пройдёт позже: участник может нажать «Проверить» на экране провайдера.' };
}

function actionError(err, did) {
  if (err.status === 422 && err.code === 'unreachable') return { title: 'Имя не разрешается', text: `Сервер не получил ответ DNS для этого адреса. ${did} Повторите позже.` };
  if (err.status === 400) return { title: 'Запрос не принят', text: `Сервер отклонил список адресов. ${did} Обновите список и попробуйте ещё раз.` };
  if (err.status === 404) return { title: 'Запроса уже нет', text: 'Провайдер удалён или запрос снят. Обновите список.' };
  if (err.status === 403) return { title: 'Нужны права администратора', text: did };
  return failure(err, `${did} `);
}

export async function viewProviderRequests() {
  const c = context();
  const base = { title: 'Запросы на внутренние адреса', subtitle: 'Настройки', backHref: '#/settings' };
  if (c.session.user.role !== 'admin') {
    await c.frame({ ...base, body: `<div class="page-narrow">${forbiddenState('Запросы на внутренние адреса разбирает администратор сервера.')}</div>` });
    return;
  }
  await c.frame({
    ...base,
    body: '<div class="page-wide stack gap-4"><div class="form-alert" id="pr-alert"></div><div id="pr-note" role="status" aria-live="polite"></div><div class="stack gap-3" id="pr-content" aria-busy="true"></div></div>',
  });
  const content = $('#pr-content');
  const alertBox = $('#pr-alert');
  const note = $('#pr-note');
  let rows = [];
  const rejected = new Set(); // отказали в этой сессии: ядро оставляет провайдера в pending_admin, отдельного статуса нет

  const setNote = (title, text) => {
    note.innerHTML = title ? `<div class="banner banner-info"><span class="banner-icon">${ICONS.check}</span><span class="banner-text"><span class="banner-title">${esc(title)}</span><span class="banner-sub">${esc(text)}</span></span></div>` : '';
  };

  function paint() {
    content.removeAttribute('aria-busy');
    if (!rows.length) {
      content.innerHTML = stateHtml({ iconHtml: ICONS.check, title: 'Запросов нет', text: 'Когда кто-то укажет адрес во внутренней сети, запрос появится здесь. Сервер сам внутрь сети не ходит, пока вы не разрешите.' });
      return;
    }
    content.innerHTML = `<h2 class="section-label" id="pr-h">Запросы · ${rows.length}</h2>
      <ul class="list-plain" aria-labelledby="pr-h">${rows.map((r) => requestRow(r, rejected.has(r.id) && !r.allow_private)).join('')}</ul>
      <p class="t-footnote">Разрешение действует только для тех IP-адресов, которые вы видите. Если имя сервера станет указывать на другие, запрос вернётся на повторное одобрение.</p>`;
  }

  async function load() {
    content.setAttribute('aria-busy', 'true');
    if (!rows.length) content.innerHTML = loadingHtml('Загружаю запросы');
    try {
      rows = await api.listProviderRequests();
    } catch (err) {
      if (err.status === 401) return;
      content.removeAttribute('aria-busy');
      if (err.status === 403) { content.innerHTML = forbiddenState('Запросы на внутренние адреса разбирает администратор сервера.'); return; }
      content.innerHTML = stateHtml({ iconHtml: ICONS.cloudOff, title: 'Запросы не загрузились', text: `${failure(err).text} Решения по адресам не менялись.`, actions: retryButton, kind: 'state-error' });
      return;
    }
    paint();
  }

  // Лист подтверждения. Отправляется копия того, что показано в листе: список между показом и отправкой не перечитывается.
  function openAllow(row) {
    const shown = { id: row.id, name: row.name, email: row.email, base_url: row.base_url, ips: [...row.resolved_ips], reapproval: !!row.reapproval };
    const dlg = openDialog({
      title: shown.reapproval ? 'Разрешить заново?' : 'Разрешить внутренний адрес?',
      subtitle: `${shown.name} · ${shown.email}`,
      content: `<div class="stack gap-3">
        <div class="banner banner-attention" role="note"><span class="banner-icon">${ICONS.alert}</span><span class="banner-text"><span class="banner-title">Проверьте адрес и IP-адреса</span><span class="banner-sub">Сервер получит доступ к этим адресам во внутренней сети. Разрешайте только те, которые знаете.</span></span></div>
        <dl class="compare-list">
          <dt>Адрес</dt><dd class="t-log wrap" data-allow-url data-i18n-skip>${esc(shown.base_url)}</dd>
          <dt>IP-адреса, которые будут разрешены</dt><dd>${ipList(shown.ips, ' data-allow-ips')}</dd>
        </dl>
        <div class="form-alert" id="cf-alert"></div>
        <div class="btn-row btn-row-2"><button type="button" class="btn btn-secondary" data-close>Отмена</button><button type="button" class="btn btn-primary" data-confirm>Разрешить</button></div>
      </div>`,
    });
    let outcome = null;
    wireConfirm(dlg, {
      run: async () => {
        try {
          outcome = { result: await api.allowPrivateProvider(shown.id, { allow: true, base_url: shown.base_url, ips: shown.ips }) };
        } catch (err) {
          if (err.status === 409) { outcome = { stale: true }; return; }
          throw err;
        }
      },
      describeError: (err) => actionError(err, 'Адрес не разрешён.'),
      done: () => {
        if (outcome && outcome.stale) {
          setNote('', '');
          setAlert(alertBox, 'Адрес изменился, проверьте ещё раз', 'Пока вы смотрели запрос, адрес или IP-адреса изменились. Список обновлён: сверьте их и разрешите заново, если всё верно.');
        } else {
          alertBox.innerHTML = '';
          const text = approvedNote(shown.name, outcome.result.status);
          setNote(text.title, text.text);
        }
        load();
      },
    });
  }

  function openReject(row) {
    const reapproval = !!row.reapproval;
    confirmAction({
      title: reapproval ? 'Отозвать разрешение?' : 'Отклонить запрос?',
      subtitle: `${row.name} · ${row.email}`,
      text: `${reapproval ? 'Прежнее разрешение снимется. ' : 'Адрес не будет разрешён. '}Провайдер останется в состоянии «Ждёт одобрения администратора»: участник увидит это в своих настройках, боты на его моделях не заработают. Позже адрес можно разрешить.`,
      confirmLabel: reapproval ? 'Отозвать' : 'Отклонить',
      run: () => api.allowPrivateProvider(row.id, { allow: false }),
      describeError: (err) => actionError(err, 'Запрос не изменён.'),
      done: () => {
        rejected.add(row.id);
        alertBox.innerHTML = '';
        setNote(`Адрес не разрешён: ${row.name}`, 'Участник видит провайдера в состоянии «Ждёт одобрения администратора».');
        load();
      },
    });
  }

  content.addEventListener('click', (e) => {
    if (e.target.closest('[data-act="retry"]')) { load(); return; }
    const allow = e.target.closest('[data-allow]');
    if (allow) { const row = rows.find((r) => r.id === allow.getAttribute('data-allow')); if (row) openAllow(row); return; }
    const reject = e.target.closest('[data-reject]');
    if (reject) { const row = rows.find((r) => r.id === reject.getAttribute('data-reject')); if (row) openReject(row); }
  });
  await load();
}
