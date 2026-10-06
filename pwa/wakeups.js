// pwa/wakeups.js: карточка «Пробуждения бота» в настройках бота (docs/contracts.md, раздел 17).
// Показывает активные пробуждения, которые бот запланировал себе сам, и даёт отменить любое из них.

import * as api from './api.js';
import { esc, fmtDateTime } from './ui.js';

const REASON_SHOWN = 120;

export function wakeupsCardHtml() {
  return `<div class="card card-pad stack gap-3" data-wakeups>
        <div class="stack gap-1">
          <h2 class="t-headline" style="font-size:15px;margin:0;">Пробуждения бота</h2>
          <span class="t-footnote">Бот сам просит разбудить его позже. Отмена убирает пробуждение, бот о нём не узнает.</span>
        </div>
        <div class="stack gap-2" data-wakeups-body aria-live="polite"><span class="t-footnote">Загрузка…</span></div>
      </div>`;
}

function label(wakeup) {
  const text = (wakeup.reason || wakeup.prompt || '').replace(/\s+/g, ' ').trim();
  return text.length > REASON_SHOWN ? `${text.slice(0, REASON_SHOWN - 1)}…` : text;
}

function rowHtml(wakeup) {
  const due = new Date(wakeup.scheduled_at).getTime() <= Date.now();
  return `<div class="row gap-3 wk-row" data-wk-id="${esc(wakeup.id)}">
    <span class="flex-1 stack min-w-0">
      <span class="row-title wk-reason" data-i18n-skip>${esc(label(wakeup))}</span>
      <span class="row-sub">${esc(fmtDateTime(wakeup.scheduled_at))}${due ? ' · <span>ждёт запуска</span>' : ''}</span>
    </span>
    <button type="button" class="btn btn-ghost" data-wk-cancel="${esc(wakeup.id)}" aria-label="Отменить пробуждение: ${esc(label(wakeup))}">Отменить</button>
  </div>`;
}

function errorHtml(text) {
  return `<div class="stack gap-2 wk-error-box"><span class="t-footnote wk-error" role="alert" style="color:var(--attention-text);">${esc(text)}</span>
    <button type="button" class="btn btn-secondary" data-wk-retry>Повторить</button></div>`;
}

// Подключает карточку внутри scope (экран настроек бота). Устаревший ответ (экран уже перерисован) ничего не рисует.
export function mountWakeups(scope, botId) {
  const card = scope && scope.querySelector('[data-wakeups]');
  const body = card && card.querySelector('[data-wakeups-body]');
  if (!body) return;
  let items = [];
  let busy = false;

  const paint = (error) => {
    if (error) { body.innerHTML = errorHtml(error); return; }
    body.innerHTML = items.length
      ? items.map(rowHtml).join('')
      : '<span class="t-footnote wk-empty">Бот пока не планировал пробуждений</span>';
  };

  async function load() {
    body.innerHTML = '<span class="t-footnote">Загрузка…</span>';
    try {
      const list = await api.listWakeups(botId);
      if (!card.isConnected) return;
      items = list.filter((w) => w.status === 'active');
      paint();
    } catch {
      if (card.isConnected) paint('Не удалось загрузить пробуждения');
    }
  }

  async function cancel(button) {
    if (busy) return;
    busy = true;
    button.disabled = true;
    try {
      await api.cancelWakeup(button.getAttribute('data-wk-cancel'));
      if (!card.isConnected) return;
      items = items.filter((w) => w.id !== button.getAttribute('data-wk-cancel'));
      paint();
    } catch (err) {
      if (!card.isConnected) return;
      // 404 и 409: пробуждение уже сработало или исчезло, список просто устарел
      if (err && (err.status === 404 || err.status === 409)) await load();
      else { button.disabled = false; paint('Не удалось отменить пробуждение'); }
    } finally {
      busy = false;
    }
  }

  card.addEventListener('click', (event) => {
    const target = event.target instanceof Element ? event.target : null;
    const retry = target && target.closest('[data-wk-retry]');
    if (retry) { load(); return; }
    const button = target && target.closest('[data-wk-cancel]');
    if (button) cancel(button);
  });
  load();
}
