// groups.js: «Обсуждения»: боты по очереди отвечают в одном общем треде (контракт этапа 11).
// Экраны: список #/groups, создание #/groups/new, тред группы #/groups/<id>. Каркас (шапка, боковая панель) даёт app.js.
import * as api from './api.js';
import { openThreadStream } from './ws.js';
import { avatarHtml } from './avatars.js';
import { ICONS, esc, alertHtml } from './ui.js';
import { confirmAction, stateHtml, retryButton } from './account.js';

const MODES = {
  round: { title: 'По кругу', text: 'Каждый бот отвечает по разу в раунде. Раундов столько, сколько задано.' },
  debate: { title: 'Спор', text: 'Как по кругу, но боты могут закончить раньше: если все согласны или передали слово.' },
  moderated: { title: 'С модератором', text: 'После каждого раунда говорит модератор. Он решает, продолжать ли, и пишет итог.' },
};
const RUN_LABEL = { done: 'Завершено', stopped: 'Остановлено', failed: 'Ошибка' };
const MIN_BOTS = 2;
const MAX_BOTS = 6;
// Последняя строка ответа модератора {"next":..,"done":..} служебная, в ленте её не показываем.
const MODERATOR_JSON = /\s*\{\s*"next"\s*:[^\n]*\}\s*$/;
const GROUP_REPLY_MARKER = /^\s*\[(AGREE|СОГЛАСЕН|PASS|ПАС)\]\s*/i;

export function parseGroupMarker(text) {
  const source = typeof text === 'string' ? text : '';
  const match = GROUP_REPLY_MARKER.exec(source);
  if (!match) return { marker: null, text: source };
  const word = match[1].toUpperCase();
  return {
    marker: word === 'AGREE' || word === 'СОГЛАСЕН' ? 'agree' : 'pass',
    text: source.slice(match[0].length),
  };
}

const byPosition = (members) => members.slice().sort((a, b) => a.position - b.position);

function avatarsHtml(members, size = 28) {
  return `<span class="group-avatars" aria-hidden="true">${byPosition(members).slice(0, 3).map((m) => avatarHtml(m.avatar || 'robot', null, size)).join('')}</span>`;
}
function modeLine(group) {
  return `<span>${esc(MODES[group.mode] ? MODES[group.mode].title : group.mode)}</span>`;
}
function runLabelHtml(run) {
  if (!run) return '<span>Ещё не запускалось</span>';
  if (run.status === 'running') return `<span>Идёт раунд ${Number(run.round) || 1} из ${Number(run.max_rounds) || 1}</span>`;
  return `<span>${esc(RUN_LABEL[run.status] || run.status)}</span>`;
}
// Причина остановки из ядра → человеческая подпись. bot_error:<код> разбирается отдельно (код показывается как есть).
const STOP_REASON = {
  max_rounds: 'Раунды закончились',
  agreed: 'Достигли согласия',
  moderator_done: 'Модератор подвёл итог',
  budget: 'Кончился бюджет токенов',
  stopped: 'Остановлено вами',
  core_restart: 'Перезапуск сервера',
  no_participants: 'Никто из ботов не смог ответить',
};
function stopReasonHtml(reason) {
  if (!reason) return '';
  const sep = ' <span>·</span> ';
  if (reason.startsWith('bot_auth_expired:')) return `${sep}<span>У бота истёк вход в подписку</span>`;
  if (reason.startsWith('bot_error')) {
    const code = reason.slice('bot_error'.length).replace(/^:/, '');
    return `${sep}<span>Ошибка у бота${code ? ':' : ''}</span>${code ? ` <span data-i18n-skip>${esc(code)}</span>` : ''}`;
  }
  return STOP_REASON[reason] ? `${sep}<span>${esc(STOP_REASON[reason])}</span>` : '';
}

export function createGroups({ app, frame, desktopPage, setCleanup, isDesktop }) {
  // -------------------------------------------------------------------------
  // Список
  // -------------------------------------------------------------------------
  async function viewList() {
    let groups;
    try { groups = await api.listGroups(); } catch (err) {
      if (err && err.status === 401) return;
      await frame({ title: 'Обсуждения', backHref: '#/', activeNav: 'groups', body: stateHtml({ iconHtml: ICONS.alert, title: 'Не удалось загрузить обсуждения', text: 'Проверьте сеть и повторите.', actions: retryButton, kind: 'state-error' }) });
      app.querySelector('[data-act="retry"]')?.addEventListener('click', () => viewList());
      return;
    }
    const create = `<a href="#/groups/new" class="btn btn-primary" data-group-new>${ICONS.plus}Новое обсуждение</a>`;
    const rows = groups.map((g) => `<a class="list-row group-row" href="#/groups/${encodeURIComponent(g.id)}" data-group-id="${esc(g.id)}">
      ${avatarsHtml(g.members)}
      <span class="row-body">
        <span class="row-title" data-i18n-skip>${esc(g.title)}</span>
        <span class="row-sub">${modeLine(g)} · <span data-i18n-skip>${esc(byPosition(g.members).map((m) => m.name).join(', '))}</span></span>
      </span>
      <span class="row-meta group-run" data-run-status="${esc(g.last_run ? g.last_run.status : 'none')}">${runLabelHtml(g.last_run)}</span>
    </a>`).join('');
    const empty = stateHtml({ iconHtml: ICONS.users, title: 'Обсуждений пока нет', text: 'Соберите из 2–6 своих ботов общий тред: они отвечают по очереди и видят реплики друг друга.', actions: create });
    const desktop = isDesktop();
    await frame({
      title: 'Обсуждения', subtitle: 'Боты отвечают по очереди в общем треде', backHref: '#/', activeNav: 'groups',
      body: groups.length ? `<div class="stack gap-3 group-list">${desktop ? `<div>${create}</div>` : ''}${rows}</div>` : empty,
      mobileActions: groups.length ? create : '',
    });
  }

  // -------------------------------------------------------------------------
  // Создание
  // -------------------------------------------------------------------------
  async function viewNew() {
    let bots;
    try { bots = await api.listBots(); } catch (err) {
      if (err && err.status === 401) return;
      await frame({ title: 'Новое обсуждение', backHref: '#/groups', activeNav: 'groups', body: stateHtml({ iconHtml: ICONS.alert, title: 'Не удалось загрузить ботов', text: 'Проверьте сеть и повторите.', actions: retryButton, kind: 'state-error' }) });
      app.querySelector('[data-act="retry"]')?.addEventListener('click', () => viewNew());
      return;
    }
    const picked = []; // порядок выбора = порядок ответов (position)
    let mode = 'debate';
    const botButtons = bots.map((b) => `<button type="button" class="group-bot" role="checkbox" aria-checked="false" data-bot-id="${esc(b.id)}">
      ${avatarHtml(b.avatar || 'robot', null, 36)}
      <span class="stack min-w-0 group-bot-text"><span class="row-title" data-i18n-skip>${esc(b.name)}</span><span class="row-sub" data-i18n-skip>${esc(b.role || '')}</span></span>
      <span class="group-bot-order" aria-hidden="true"></span>
    </button>`).join('');
    const modeCards = Object.entries(MODES).map(([key, m]) => `<button type="button" role="radio" class="group-mode" aria-checked="${key === mode}" data-mode="${key}">
      <span class="row-title">${esc(m.title)}</span><span class="t-footnote">${esc(m.text)}</span></button>`).join('');
    const body = bots.length < MIN_BOTS
      ? `<div class="banner banner-info" role="status"><span class="banner-icon">${ICONS.alert}</span><span class="banner-text"><span class="banner-title">Нужно минимум два бота</span><span class="banner-sub">Создайте ещё одного бота и вернитесь.</span></span><a class="btn btn-secondary" href="#/bots/new">Создать бота</a></div>`
      : `<form id="group-form" class="card card-pad stack gap-4 group-form" novalidate>
        <div class="form-field">
          <label for="gr-title">Название</label>
          <input id="gr-title" class="input" type="text" maxlength="120" autocomplete="off" placeholder="О чём спорят боты">
        </div>
        <div class="form-field">
          <span class="group-label" id="gr-bots-label">Участники</span>
          <div class="group-bots" role="group" aria-labelledby="gr-bots-label">${botButtons}</div>
          <span class="field-note" id="gr-bots-note" aria-live="polite"></span>
        </div>
        <div class="form-field">
          <span class="group-label" id="gr-mode-label">Режим</span>
          <div class="group-modes" role="radiogroup" aria-labelledby="gr-mode-label">${modeCards}</div>
        </div>
        <div class="form-field">
          <label for="gr-rounds">Раундов</label>
          <input id="gr-rounds" class="input" type="number" inputmode="numeric" min="1" max="10" step="1" value="3">
          <span class="field-note">От 1 до 10.</span>
        </div>
        <div class="form-field" id="gr-moderator-field" hidden>
          <label for="gr-moderator">Модератор</label>
          <select id="gr-moderator" class="input"></select>
        </div>
        <div class="form-field">
          <label for="gr-budget">Бюджет токенов (необязательно)</label>
          <input id="gr-budget" class="input" type="number" inputmode="numeric" min="1000" max="2000000" step="1" placeholder="Без ограничения">
          <span class="field-note">Обсуждение остановится, когда боты потратят столько токенов. От 1000 до 2 000 000.</span>
        </div>
        <div id="gr-alert" class="form-alert"></div>
        <button type="submit" id="gr-submit" class="btn btn-primary" disabled>Создать обсуждение</button>
      </form>`;
    await frame({ title: 'Новое обсуждение', subtitle: 'Выберите ботов и режим', backHref: '#/groups', activeNav: 'groups', body });
    const form = app.querySelector('#group-form');
    if (!form) return;
    const $ = (sel) => form.querySelector(sel);
    const titleEl = $('#gr-title');
    const roundsEl = $('#gr-rounds');
    const budgetEl = $('#gr-budget');
    const modEl = $('#gr-moderator');
    const submit = $('#gr-submit');
    const botName = (id) => (bots.find((b) => b.id === id) || {}).name || id;

    function valid() {
      const rounds = Number(roundsEl.value);
      const budgetText = budgetEl.value.trim();
      const budget = Number(budgetText);
      return titleEl.value.trim().length >= 1 && titleEl.value.trim().length <= 120
        && picked.length >= MIN_BOTS && picked.length <= MAX_BOTS
        && Number.isInteger(rounds) && rounds >= 1 && rounds <= 10
        && (!budgetText || (Number.isInteger(budget) && budget >= 1000 && budget <= 2000000))
        && (mode !== 'moderated' || picked.includes(modEl.value));
    }
    function paint() {
      form.querySelectorAll('.group-bot').forEach((btn) => {
        const index = picked.indexOf(btn.dataset.botId);
        btn.setAttribute('aria-checked', String(index >= 0));
        btn.querySelector('.group-bot-order').textContent = index >= 0 ? String(index + 1) : '';
        btn.disabled = index < 0 && picked.length >= MAX_BOTS;
      });
      form.querySelectorAll('.group-mode').forEach((btn) => btn.setAttribute('aria-checked', String(btn.dataset.mode === mode)));
      $('#gr-bots-note').innerHTML = `<span>Выбрано: ${picked.length} из ${MAX_BOTS} (нужно от ${MIN_BOTS})</span>`;
      const moderatorField = $('#gr-moderator-field');
      moderatorField.hidden = mode !== 'moderated';
      const current = modEl.value;
      modEl.innerHTML = picked.map((id) => `<option value="${esc(id)}" data-i18n-skip>${esc(botName(id))}</option>`).join('');
      if (picked.includes(current)) modEl.value = current;
      submit.disabled = !valid();
    }
    form.querySelector('.group-bots').addEventListener('click', (e) => {
      const btn = e.target.closest('.group-bot');
      if (!btn) return;
      const id = btn.dataset.botId;
      const index = picked.indexOf(id);
      if (index >= 0) picked.splice(index, 1);
      else if (picked.length < MAX_BOTS) picked.push(id);
      paint();
    });
    form.querySelector('.group-modes').addEventListener('click', (e) => {
      const btn = e.target.closest('.group-mode');
      if (!btn) return;
      mode = btn.dataset.mode;
      paint();
    });
    form.addEventListener('input', paint);
    form.addEventListener('change', paint);
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      if (!valid()) return;
      const payload = { title: titleEl.value.trim(), bot_ids: picked.slice(), mode, max_rounds: Number(roundsEl.value) };
      if (mode === 'moderated') payload.moderator_bot_id = modEl.value;
      if (budgetEl.value.trim()) payload.token_budget = Number(budgetEl.value);
      submit.disabled = true;
      submit.setAttribute('aria-busy', 'true');
      $('#gr-alert').innerHTML = '';
      try {
        const group = await api.createGroup(payload);
        location.hash = `#/groups/${encodeURIComponent(group.id)}`;
      } catch (err) {
        if (err && err.status === 401) return;
        submit.removeAttribute('aria-busy');
        const detail = err && err.status === 422 ? 'Проверьте название, участников и числа.' : (err && err.status === 404 ? 'Одного из ботов уже нет. Обновите страницу.' : 'Повторите попытку.');
        $('#gr-alert').innerHTML = alertHtml('Не удалось создать обсуждение', detail);
        $('#gr-alert').firstElementChild.focus();
        submit.disabled = !valid();
      }
    });
    paint();
  }

  // -------------------------------------------------------------------------
  // Тред группы
  // -------------------------------------------------------------------------
  async function viewGroup(id) {
    let group;
    try { group = await api.getGroup(id); } catch (err) {
      if (err && err.status === 401) return;
      if (err && err.status === 404) {
        await frame({ title: 'Обсуждение', backHref: '#/groups', activeNav: 'groups', body: stateHtml({ iconHtml: ICONS.alert, title: 'Обсуждение не найдено', text: 'Возможно, его удалили.', actions: '<a class="btn btn-secondary" href="#/groups">К списку</a>' }) });
        return;
      }
      throw err;
    }
    const members = byPosition(group.members || []);
    const memberById = new Map(members.map((m) => [m.bot_id, m]));
    const modeTitle = MODES[group.mode] ? MODES[group.mode].title : group.mode;
    const names = members.map((m) => m.name).join(', ');
    const desktop = isDesktop();
    const trashBtn = `<button type="button" class="${desktop ? 'btn btn-secondary' : 'icon-btn sunken'}" data-group-delete aria-label="Удалить обсуждение">${ICONS.trash}${desktop ? 'Удалить' : ''}</button>`;
    const composer = `<form class="${desktop ? 'desktop-composer' : 'composer'} group-composer" id="group-form" novalidate>
      <div class="group-alert" id="group-alert"></div>
      <div class="${desktop ? 'group-composer-row' : 'composer-row2'}">
        <label class="sr-only" for="group-input">Сообщение</label>
        <input id="group-input" class="composer-input" type="text" autocomplete="off" placeholder="Тема или вопрос для обсуждения" maxlength="8000" disabled>
        <button type="submit" id="group-send" aria-label="Отправить" disabled class="icon-btn round" style="background:var(--bg-emphasis);color:var(--fg-on-emphasis);">${ICONS.send}</button>
      </div>
    </form>`;
    const statusBar = `<div class="group-status" id="group-status" role="status" aria-live="polite" hidden>
      <span class="spin" aria-hidden="true">${ICONS.spinner}</span>
      <span class="group-status-text flex-1 min-w-0" id="group-status-text"></span>
      <button type="button" class="btn btn-danger group-stop" id="group-stop">${ICONS.stop}Остановить</button>
    </div>`;
    const headInfo = `<span class="t-footnote" style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis;"><span>${esc(modeTitle)}</span> · <span data-i18n-skip>${esc(names)}</span></span>`;
    if (desktop) {
      await desktopPage({
        activeNav: 'groups',
        mainHtml: `<main class="desktop-main">
          <div class="desktop-thread-head">${avatarsHtml(members, 32)}
            <div class="flex-1 min-w-0 stack"><h1 class="t-headline header-title" data-i18n-skip>${esc(group.title)}</h1>${headInfo}</div>
            ${trashBtn}
          </div>
          ${statusBar}
          <div id="thread-body" class="desktop-thread-body group-body"></div>
          ${composer}
        </main>`,
      });
    } else {
      app.innerHTML = `<div class="screen">
        <header class="app-header">
          <a href="#/groups" aria-label="Назад" class="icon-btn">${ICONS.back}</a>
          ${avatarsHtml(members, 28)}
          <div class="flex-1 min-w-0 stack"><h1 class="t-headline header-title" data-i18n-skip>${esc(group.title)}</h1>${headInfo}</div>
          ${trashBtn}
        </header>
        ${statusBar}
        <div id="thread-body" class="thread-body group-body"></div>
        ${composer}
      </div>`;
    }
    const body = app.querySelector('#thread-body');
    const statusEl = app.querySelector('#group-status');
    const statusText = app.querySelector('#group-status-text');
    const stopBtn = app.querySelector('#group-stop');
    const input = app.querySelector('#group-input');
    const sendBtn = app.querySelector('#group-send');
    const alertBox = app.querySelector('#group-alert');
    let disposed = false;
    setCleanup(() => { disposed = true; });

    // Состояние обсуждения собирается из событий треда: group_status, group_round, реплики ботов.
    const state = { running: false, stopping: false, round: 1, maxRounds: group.max_rounds || 1, replied: [], turnBot: null, summarySeen: false, seenStatus: false, statusCount: 0, connected: false };
    const bubbles = new Map(); // turn_id → { parts, el, botId }
    let lastModeratorBubble = null;
    const isModeratorMsg = (p) => p.role === 'moderator';

    function answeringName() {
      if (state.turnBot) return state.turnBot.name;
      const open = Array.from(bubbles.values()).find((b) => b.partial);
      if (open) return (memberById.get(open.botId) || {}).name || open.name || '';
      // запасной вариант без group_turn: по порядку position
      const next = members.find((m) => !state.replied.includes(m.bot_id));
      if (next) return next.name;
      if (group.mode === 'moderated' && state.replied.length >= members.length && group.moderator_bot_id) return (memberById.get(group.moderator_bot_id) || {}).name || '';
      return '';
    }
    function paintStatus() {
      statusEl.hidden = !state.running;
      if (state.running) {
        const who = state.stopping ? '' : answeringName();
        statusText.innerHTML = state.stopping
          ? '<span>Останавливаю…</span>'
          : `<span>Идёт раунд ${state.round} из ${state.maxRounds}${who ? ' · отвечает' : ''}</span>${who ? ` <span data-i18n-skip>${esc(who)}</span>` : ''}`;
      }
      stopBtn.disabled = state.stopping;
      const locked = state.running || !state.connected;
      input.disabled = locked;
      sendBtn.disabled = locked;
      input.placeholder = state.running ? 'Идёт обсуждение, ждите итога' : 'Тема или вопрос для обсуждения';
    }
    function pill(html, attrs = '') {
      body.insertAdjacentHTML('beforeend', `<div class="system-pill" ${attrs}>${html}</div>`);
    }
    function bubbleText(parts) {
      const finals = parts.filter((p) => p.final);
      const last = finals[finals.length - 1];
      return last && last.text ? last.text : parts.map((p) => p.text).filter(Boolean).join('');
    }
    function ensureBubble(ev) {
      const p = ev.payload || {};
      const key = ev.turn_id != null && ev.turn_id !== '' ? String(ev.turn_id) : `seq-${ev.seq}`;
      let entry = bubbles.get(key);
      if (!entry) {
        const member = memberById.get(p.bot_id) || {};
        const name = p.bot_name || member.name || 'Бот';
        const mod = isModeratorMsg(p);
        body.insertAdjacentHTML('beforeend', `<div class="msg-group${mod ? ' is-moderator' : ''}" data-bot-id="${esc(p.bot_id || '')}" data-turn-id="${esc(key)}">
          <span class="msg-group-avatar">${avatarHtml(member.avatar || 'robot', null, 32)}</span>
          <div class="msg-bot msg-group-bubble">
            <span class="msg-group-head"><span class="msg-group-name" data-i18n-skip>${esc(name)}</span><span class="msg-group-tag msg-group-marker" hidden></span>${mod ? '<span class="msg-group-tag msg-group-moderator-tag">Модератор</span>' : ''}</span>
            <span class="msg-bot-text"><span class="msg-group-content" data-i18n-skip></span><em class="msg-group-passed" hidden>Пропустил ход</em><span class="t-muted msg-group-streaming" hidden>…</span></span>
          </div>
        </div>`);
        entry = { el: body.lastElementChild, parts: [], botId: p.bot_id, name, partial: true, moderator: mod };
        bubbles.set(key, entry);
        if (!mod && !state.replied.includes(p.bot_id)) state.replied.push(p.bot_id);
      }
      return entry;
    }
    function markSummary(el) {
      state.summarySeen = true;
      el.classList.add('is-summary');
      const tag = el.querySelector('.msg-group-moderator-tag');
      if (tag) tag.textContent = 'Итог модератора';
    }
    function onAssistant(ev) {
      const p = ev.payload || {};
      const entry = ensureBubble(ev);
      entry.parts.push({ seq: ev.seq || 0, text: typeof p.text === 'string' ? p.text : '', final: p.final !== false });
      entry.parts.sort((a, b) => a.seq - b.seq);
      entry.partial = !entry.parts.some((x) => x.final);
      let text = bubbleText(entry.parts);
      if (entry.moderator) text = text.replace(MODERATOR_JSON, '');
      const parsed = parseGroupMarker(text);
      const marker = entry.el.querySelector('.msg-group-marker');
      if (marker) {
        marker.textContent = parsed.marker === 'agree' ? 'согласен' : parsed.marker === 'pass' ? 'пропускает ход' : '';
        marker.hidden = !parsed.marker;
      }
      const passed = parsed.marker === 'pass' && !parsed.text.trim() && !entry.partial;
      const content = entry.el.querySelector('.msg-group-content');
      content.textContent = passed ? '' : parsed.text;
      content.hidden = passed;
      entry.el.querySelector('.msg-group-passed').hidden = !passed;
      entry.el.querySelector('.msg-group-streaming').hidden = !entry.partial;
      if (!entry.partial && state.turnBot && state.turnBot.botId === entry.botId) state.turnBot = null;
      if (entry.moderator) {
        lastModeratorBubble = entry.el;
        if (p.summary === true) markSummary(entry.el);
      }
    }
    function onStatus(p) {
      state.seenStatus = true;
      state.statusCount += 1;
      if (p.round) state.round = p.round;
      if (p.status === 'running') {
        state.running = true;
        state.stopping = false;
        return;
      }
      state.running = false;
      state.stopping = false;
      state.turnBot = null;
      const extra = stopReasonHtml(p.stop_reason);
      if (p.status === 'done') {
        pill(`<span>Обсуждение завершено</span>${extra}`, 'data-group-event="done"');
        if (lastModeratorBubble && !state.summarySeen) markSummary(lastModeratorBubble); // страховка: ядро без summary
      } else if (p.status === 'stopped') pill(`<span>Обсуждение остановлено</span>${extra}`, 'data-group-event="stopped"');
      else if (p.status === 'failed') pill(`<span>Обсуждение прервано</span>${extra}`, 'data-group-event="failed" style="color:var(--danger-fg);"');
      bubbles.forEach((b) => { b.partial = false; });
      lastModeratorBubble = null;
      state.summarySeen = false;
    }
    function handle(ev) {
      const p = ev.payload || {};
      switch (ev.kind) {
        case 'user_msg':
          body.insertAdjacentHTML('beforeend', `<div class="msg-user" data-i18n-skip>${esc(p.text)}</div>`);
          break;
        case 'assistant_msg':
          onAssistant(ev);
          break;
        case 'group_round':
          state.round = p.round || state.round;
          state.maxRounds = p.max_rounds || state.maxRounds;
          state.replied = [];
          state.turnBot = null;
          body.insertAdjacentHTML('beforeend', `<div class="group-divider" role="separator" data-round="${Number(p.round) || ''}"><span>Раунд ${Number(p.round) || ''} из ${Number(p.max_rounds) || state.maxRounds}</span></div>`);
          break;
        case 'group_turn':
          if (p.round) state.round = p.round;
          state.turnBot = { botId: p.bot_id, name: p.bot_name || (memberById.get(p.bot_id) || {}).name || '' };
          break;
        case 'group_status':
          onStatus(p);
          break;
        case 'system':
          pill(`<span data-i18n-skip>${esc(p.text)}</span>`);
          break;
        default:
          break; // usage, status ходов и прочее в общем треде не показываем
      }
    }

    let events = [];
    try { events = await api.getEvents(group.id); } catch { /* хвост дочитает поток */ }
    events = (events || []).slice().sort((a, b) => a.seq - b.seq);
    for (const ev of events) handle(ev);
    if (!state.seenStatus && group.last_run && group.last_run.status === 'running') {
      state.running = true;
      state.round = group.last_run.round || 1;
    }
    body.scrollTop = body.scrollHeight;
    const since = events.reduce((last, ev) => Math.max(last, ev.seq || 0), 0);
    const stopStream = openThreadStream(group.id, since, (ev) => {
      if (disposed) return;
      handle(ev);
      paintStatus();
      body.scrollTop = body.scrollHeight;
    });
    setCleanup(stopStream);
    state.connected = true;
    paintStatus();

    function showAlert(title, text) {
      alertBox.innerHTML = alertHtml(title, text);
    }
    app.querySelector('#group-form').addEventListener('submit', async (e) => {
      e.preventDefault();
      const text = input.value.trim();
      if (!text || state.running) return;
      alertBox.innerHTML = '';
      sendBtn.disabled = true;
      const before = state.statusCount;
      try {
        await api.sendGroupMessage(group.id, text);
        input.value = '';
        if (state.statusCount === before) state.running = true; // статус мог прийти из потока раньше ответа
        paintStatus();
      } catch (err) {
        if (err && err.status === 401) return;
        if (err && err.status === 409) {
          state.running = true;
          paintStatus();
          showAlert('Обсуждение уже идёт', 'Дождитесь итога или остановите его.');
        } else {
          showAlert('Не удалось отправить', err && err.status === 422 ? 'Сообщение пустое или длиннее 8000 знаков.' : 'Повторите попытку.');
          paintStatus();
        }
      }
    });
    stopBtn.addEventListener('click', async () => {
      state.stopping = true;
      paintStatus();
      alertBox.innerHTML = '';
      try {
        await api.stopGroup(group.id);
        state.running = false;
        state.stopping = false;
      } catch (err) {
        state.stopping = false;
        if (err && err.status === 409) state.running = false; // уже закончилось
        else if (!(err && err.status === 401)) showAlert('Не удалось остановить', 'Повторите попытку.');
      }
      paintStatus();
    });
    app.querySelector('[data-group-delete]').addEventListener('click', () => {
      confirmAction({
        title: 'Удалить обсуждение?', subtitle: group.title, text: 'Обсуждение удалится вместе с перепиской. Боты и их обычные треды останутся.', confirmLabel: 'Удалить',
        run: () => api.deleteGroup(group.id),
        describeError: () => ({ title: 'Не удалось удалить', text: 'Остановите обсуждение и повторите.' }),
        done: () => { location.hash = '#/groups'; },
      });
    });
  }

  return { viewList, viewNew, viewGroup };
}
