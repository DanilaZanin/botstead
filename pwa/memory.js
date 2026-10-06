// memory.js: экран «Память» PWA: просмотр, фильтрация по ботам, поиск, ручное добавление,
// редактирование текста и области видимости, удаление с подтверждением, принятие и отклонение предложений ботов.
import * as api from './api.js';
import { locale } from './i18n.js';
import { ICONS, esc, fmtDate, backHeader } from './ui.js';
import {
  openDialog, confirmBody, setAlert, setBusy, loadingHtml, stateHtml, retryButton,
} from './account.js';

const app = document.getElementById('app');

function timeLabel(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  const days = Math.floor((Date.now() - d.getTime()) / 86400000);
  if (days <= 0) return 'сегодня';
  if (days === 1) return 'вчера';
  return d.toLocaleDateString(locale(), { day: 'numeric', month: 'short' }).replace('.', '');
}

// Срок из поля date: конец выбранного дня по местному времени, иначе запись истекала бы в начале этого дня (UTC).
function dateInputToIso(value) {
  return value ? new Date(`${value}T23:59:59`).toISOString() : null;
}

function formatDateForInput(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

function saveErrorText(err) {
  if (err && err.status === 400) return 'Текст не принят: он пустой, слишком длинный (больше 16 КБ) или содержит недопустимые символы.';
  if (err && err.status === 404) return 'Записи или бота уже нет. Обновите экран.';
  return (err && (err.detail || err.message)) || 'Проверьте данные и повторите попытку.';
}

export async function viewMemory() {
  let bots = [];
  let currentFilter = 'all'; // 'all' | 'shared' | '<bot_id>'
  let searchQuery = '';
  let activeTabState = 'list'; // 'list' | 'loading' | 'error'
  let memoryData = { proposed: [], active: [] };

  async function loadData() {
    activeTabState = 'loading';
    render();
    try {
      const [memories, loadedBots] = await Promise.all([
        api.listMemory(),
        api.listBots ? api.listBots() : (api.mockBots || []),
      ]);
      memoryData = memories;
      bots = loadedBots || [];
      activeTabState = 'list';
    } catch (err) {
      activeTabState = 'error';
    }
    render();
  }

  function botName(id) {
    if (!id) return 'Общая';
    const b = bots.find((x) => x.id === id);
    return b ? b.name : id;
  }

  function sourceLabel(m) {
    if (m.source === 'owner') return 'вы';
    if (m.bot_id) return botName(m.bot_id);
    if (m.source && m.source.startsWith('bot:')) return botName(m.source.slice(4));
    return m.source || 'бот';
  }

  function scopeLabel(m) {
    if (!m.bot_id) return 'Общая для всех ботов';
    return `Только ${botName(m.bot_id)}`;
  }

  function getFilteredEntries() {
    const q = searchQuery.trim().toLowerCase();
    const filterFn = (m) => {
      if (currentFilter === 'shared' && m.bot_id !== null) return false;
      if (currentFilter !== 'all' && currentFilter !== 'shared' && m.bot_id !== currentFilter) return false;
      if (q) {
        const textMatch = (m.text || '').toLowerCase().includes(q);
        const sourceMatch = sourceLabel(m).toLowerCase().includes(q);
        const botMatch = botName(m.bot_id).toLowerCase().includes(q);
        if (!textMatch && !sourceMatch && !botMatch) return false;
      }
      return true;
    };
    return {
      proposed: (memoryData.proposed || []).filter(filterFn),
      active: (memoryData.active || []).filter(filterFn),
    };
  }

  function render() {
    if (activeTabState === 'loading') {
      app.innerHTML = `<div class="screen">
        ${backHeader({ title: 'Память', subtitle: 'Загрузка…', backHref: '#/' })}
        <div class="thread-body" style="padding-top:20px;">
          ${loadingHtml('Загружаем память…')}
        </div>
      </div>`;
      return;
    }

    if (activeTabState === 'error') {
      app.innerHTML = `<div class="screen">
        ${backHeader({ title: 'Память', subtitle: 'Ошибка', backHref: '#/' })}
        <div class="thread-body" style="padding-top:20px;">
          ${stateHtml({
            iconHtml: ICONS.alert,
            title: 'Не удалось загрузить память',
            text: 'Проверьте подключение к сети и повторите попытку.',
            actions: retryButton,
            kind: 'state-error',
          })}
        </div>
      </div>`;
      const retryBtn = app.querySelector('[data-act="retry"]');
      if (retryBtn) retryBtn.addEventListener('click', () => loadData());
      return;
    }

    const { proposed, active } = getFilteredEntries();
    const totalCount = (memoryData.proposed || []).length + (memoryData.active || []).length;
    const hasAny = totalCount > 0;
    const hasFiltered = proposed.length > 0 || active.length > 0;

    const rightBtn = `<button type="button" class="btn btn-primary" data-action="add-memory" style="padding:0 14px;font-size:14px;">${ICONS.plus}Записать</button>`;

    app.innerHTML = `<div class="screen">
      ${backHeader({
        title: 'Память',
        subtitle: `Общая и по ботам · ${totalCount} фактов`,
        backHref: '#/',
        right: rightBtn,
      })}
      <div class="thread-body" style="gap:16px;">
        <div class="search-input">
          <span>${ICONS.search}</span>
          <label class="sr-only" for="ms">Поиск по памяти</label>
          <input id="ms" type="search" placeholder="Поиск по памяти" aria-label="Поиск по памяти" value="${esc(searchQuery)}">
        </div>

        <div role="tablist" aria-label="Фильтр по ботам" class="mem-tabs">
          <button type="button" role="tab" class="mem-tab${currentFilter === 'all' ? ' active' : ''}" data-filter="all" aria-selected="${currentFilter === 'all'}">Все</button>
          <button type="button" role="tab" class="mem-tab${currentFilter === 'shared' ? ' active' : ''}" data-filter="shared" aria-selected="${currentFilter === 'shared'}">Общая</button>
          ${bots.map((b) => `<button type="button" role="tab" class="mem-tab${currentFilter === b.id ? ' active' : ''}" data-filter="${esc(b.id)}" aria-selected="${currentFilter === b.id}" data-i18n-skip>${esc(b.name)}</button>`).join('')}
        </div>

        <div id="memory-content" class="stack gap-3">
          ${proposed.length ? `
            <div class="stack gap-2">
              <h2 class="section-label">Предложено ботом</h2>
              ${proposed.map((m) => `
                <div class="card card-pad stack gap-3" data-id="${esc(m.id)}">
                  <div class="t-body" style="white-space:pre-wrap;overflow-wrap:anywhere;" data-i18n-skip>${esc(m.text)}</div>
                  <div class="mem-meta">
                    <span class="mem-meta-item">${ICONS.bots}<span>Предложил: ${esc(sourceLabel(m))}</span></span>
                    <span class="mem-meta-item"><span>· ${esc(timeLabel(m.created_at))}</span></span>
                    <span class="mem-meta-item"><span>· ${esc(scopeLabel(m))}</span></span>
                    ${m.expires_at ? `<span class="mem-meta-item"><span>· до ${esc(fmtDate(m.expires_at))}</span></span>` : ''}
                  </div>
                  <div class="btn-row btn-row-2">
                    <button type="button" class="btn btn-secondary" data-action="remember-no" data-id="${esc(m.id)}">Не запоминать</button>
                    <button type="button" class="btn btn-primary" data-action="remember-yes" data-id="${esc(m.id)}">Запомнить</button>
                  </div>
                </div>
              `).join('')}
            </div>
          ` : ''}

          <div class="stack gap-2">
            <h2 class="section-label">Запомнено</h2>
            ${active.length ? active.map((m) => `
              <div class="card card-pad stack gap-2" data-id="${esc(m.id)}">
                <div class="row" style="align-items:flex-start;justify-content:space-between;gap:8px;">
                  <div class="t-body flex-1" style="white-space:pre-wrap;overflow-wrap:anywhere;" data-i18n-skip>${esc(m.text)}</div>
                  <div class="row gap-1" style="flex-shrink:0;">
                    <button type="button" class="icon-btn sunken" data-action="edit-memory" data-id="${esc(m.id)}" aria-label="Редактировать запись">${ICONS.preview}</button>
                    <button type="button" class="icon-btn sunken" data-action="delete-memory" data-id="${esc(m.id)}" aria-label="Удалить запись" style="color:var(--danger-fg);">${ICONS.trash}</button>
                  </div>
                </div>
                <div class="mem-meta">
                  <span class="mem-meta-item"><span>${esc(m.source === 'owner' ? 'Автор: вы' : `Предложил: ${sourceLabel(m)}`)}</span></span>
                  <span class="mem-meta-item"><span>· ${esc(timeLabel(m.created_at))}</span></span>
                  <span class="mem-meta-item"><span>· ${esc(scopeLabel(m))}</span></span>
                  ${m.expires_at ? `<span class="mem-meta-item"><span>· до ${esc(fmtDate(m.expires_at))}</span></span>` : '<span class="mem-meta-item"><span>· бессрочно</span></span>'}
                  ${m.version && m.version > 1 ? `<span class="mem-meta-item"><span>· ред. ${m.version}</span></span>` : ''}
                </div>
              </div>
            `).join('') : ''}
          </div>

          ${!hasFiltered ? `
            <div class="card card-pad" style="text-align:center;padding:32px 16px;">
              <p class="t-body" style="color:var(--fg-muted);margin:0 0 12px;">
                ${hasAny ? 'По вашему запросу ничего не найдено.' : 'В памяти пока нет записей. Боты сохраняют важные сведения в диалогах, или вы можете добавить запись вручную.'}
              </p>
              <button type="button" class="btn btn-secondary" data-action="add-memory">${ICONS.plus}Добавить запись</button>
            </div>
          ` : ''}
        </div>
      </div>
    </div>`;

    wireEvents();
  }

  function wireEvents() {
    const searchInput = document.getElementById('ms');
    if (searchInput) {
      searchInput.addEventListener('input', (e) => {
        searchQuery = e.target.value;
        const { proposed, active } = getFilteredEntries();
        updateListDom(proposed, active);
      });
    }

    app.querySelectorAll('[data-filter]').forEach((tab) => {
      tab.addEventListener('click', () => {
        currentFilter = tab.getAttribute('data-filter');
        render();
      });
    });

    app.querySelectorAll('[data-action="add-memory"]').forEach((btn) => {
      btn.addEventListener('click', () => openAddDialog());
    });

    app.querySelectorAll('[data-action="remember-yes"]').forEach((btn) => {
      btn.addEventListener('click', async () => {
        const id = btn.getAttribute('data-id');
        btn.disabled = true;
        try {
          await api.decideMemory(id, 'active');
          const pIdx = (memoryData.proposed || []).findIndex((m) => m.id === id);
          if (pIdx >= 0) {
            const [item] = memoryData.proposed.splice(pIdx, 1);
            item.status = 'active';
            (memoryData.active || (memoryData.active = [])).unshift(item);
          }
          render();
        } catch (err) {
          btn.disabled = false;
        }
      });
    });

    app.querySelectorAll('[data-action="remember-no"]').forEach((btn) => {
      btn.addEventListener('click', async () => {
        const id = btn.getAttribute('data-id');
        btn.disabled = true;
        try {
          await api.decideMemory(id, 'archived');
          const pIdx = (memoryData.proposed || []).findIndex((m) => m.id === id);
          if (pIdx >= 0) {
            memoryData.proposed.splice(pIdx, 1);
          }
          render();
        } catch (err) {
          btn.disabled = false;
        }
      });
    });

    app.querySelectorAll('[data-action="edit-memory"]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const id = btn.getAttribute('data-id');
        const entry = (memoryData.active || []).find((m) => m.id === id) || (memoryData.proposed || []).find((m) => m.id === id);
        if (entry) openEditDialog(entry);
      });
    });

    app.querySelectorAll('[data-action="delete-memory"]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const id = btn.getAttribute('data-id');
        const entry = (memoryData.active || []).find((m) => m.id === id) || (memoryData.proposed || []).find((m) => m.id === id);
        if (entry) openDeleteDialog(entry);
      });
    });
  }

  function updateListDom(proposed, active) {
    // Fast update without re-rendering search focus
    render();
    const searchInput = document.getElementById('ms');
    if (searchInput) {
      searchInput.focus();
      searchInput.setSelectionRange(searchQuery.length, searchQuery.length);
    }
  }

  function openAddDialog() {
    const content = `
      <form id="add-mem-form" class="stack gap-3">
        <div class="form-alert" id="add-mem-alert"></div>
        <div class="form-field">
          <label for="add-mem-text">Что нужно помнить</label>
          <textarea id="add-mem-text" class="input textarea" rows="4" placeholder="Например: Основной сервер баз данных на Ubuntu 24.04 в Белграде" aria-required="true" style="min-height:100px;"></textarea>
        </div>
        <div class="form-field">
          <label for="add-mem-bot">Кому доступно</label>
          <select id="add-mem-bot" class="input">
            <option value="">Общая (для всех ботов)</option>
            ${bots.map((b) => `<option value="${esc(b.id)}" data-i18n-skip>${esc(b.name)}</option>`).join('')}
          </select>
        </div>
        <div class="form-field">
          <label for="add-mem-exp">Срок действия (необязательно)</label>
          <input id="add-mem-exp" type="date" class="input">
        </div>
        <div class="btn-row btn-row-2" style="margin-top:8px;">
          <button type="button" class="btn btn-secondary" data-close>Отмена</button>
          <button type="button" class="btn btn-primary" id="add-mem-submit">Сохранить</button>
        </div>
      </form>
    `;

    const dlg = openDialog({
      title: 'Новая запись',
      subtitle: 'Добавить факт в память',
      content,
    });

    const submitBtn = dlg.body.querySelector('#add-mem-submit');
    const textInput = dlg.body.querySelector('#add-mem-text');
    const botSelect = dlg.body.querySelector('#add-mem-bot');
    const expInput = dlg.body.querySelector('#add-mem-exp');
    const alertBox = dlg.body.querySelector('#add-mem-alert');

    // Enter в поле даты не должен отправлять форму (перезагрузка страницы): сохранение только по кнопке.
    dlg.body.querySelector('#add-mem-form').addEventListener('submit', (e) => e.preventDefault());
    submitBtn.addEventListener('click', async () => {
      const text = textInput.value.trim();
      if (!text) {
        setAlert(alertBox, 'Заполните текст', 'Текст записи не может быть пустым.');
        textInput.focus();
        return;
      }

      setBusy(submitBtn, true, 'Сохраняем…', 'Сохранить');
      try {
        const body = {
          text,
          bot_id: botSelect.value || null,
          expires_at: dateInputToIso(expInput.value),
        };
        const created = await api.createMemory(body);
        (memoryData.active || (memoryData.active = [])).unshift(created);
        dlg.close();
        render();
      } catch (err) {
        setBusy(submitBtn, false, '', 'Сохранить');
        setAlert(alertBox, 'Не удалось сохранить', saveErrorText(err));
      }
    });
  }

  function openEditDialog(entry) {
    const content = `
      <form id="edit-mem-form" class="stack gap-3">
        <div class="form-alert" id="edit-mem-alert"></div>
        <div class="form-field">
          <label for="edit-mem-text">Текст записи</label>
          <textarea id="edit-mem-text" class="input textarea" rows="4" aria-required="true" style="min-height:100px;">${esc(entry.text)}</textarea>
        </div>
        <div class="form-field">
          <label for="edit-mem-bot">Кому доступно</label>
          <select id="edit-mem-bot" class="input">
            <option value=""${!entry.bot_id ? ' selected' : ''}>Общая (для всех ботов)</option>
            ${bots.map((b) => `<option value="${esc(b.id)}" data-i18n-skip${entry.bot_id === b.id ? ' selected' : ''}>${esc(b.name)}</option>`).join('')}
          </select>
        </div>
        <div class="form-field">
          <label for="edit-mem-exp">Срок действия</label>
          <input id="edit-mem-exp" type="date" class="input" value="${formatDateForInput(entry.expires_at)}">
        </div>
        <div class="btn-row btn-row-2" style="margin-top:8px;">
          <button type="button" class="btn btn-secondary" data-close>Отмена</button>
          <button type="button" class="btn btn-primary" id="edit-mem-submit">Сохранить</button>
        </div>
      </form>
    `;

    const dlg = openDialog({
      title: 'Редактирование записи',
      subtitle: entry.version > 1 ? `Версия ${entry.version}` : 'Изменение текста и параметров',
      content,
    });

    const submitBtn = dlg.body.querySelector('#edit-mem-submit');
    const textInput = dlg.body.querySelector('#edit-mem-text');
    const botSelect = dlg.body.querySelector('#edit-mem-bot');
    const expInput = dlg.body.querySelector('#edit-mem-exp');
    const alertBox = dlg.body.querySelector('#edit-mem-alert');

    dlg.body.querySelector('#edit-mem-form').addEventListener('submit', (e) => e.preventDefault());
    submitBtn.addEventListener('click', async () => {
      const text = textInput.value.trim();
      if (!text) {
        setAlert(alertBox, 'Заполните текст', 'Текст записи не может быть пустым.');
        textInput.focus();
        return;
      }

      setBusy(submitBtn, true, 'Сохраняем…', 'Сохранить');
      try {
        const patch = {
          text,
          bot_id: botSelect.value || null,
          expires_at: dateInputToIso(expInput.value),
        };
        const updated = await api.patchMemory(entry.id, patch);
        Object.assign(entry, updated);
        dlg.close();
        render();
      } catch (err) {
        setBusy(submitBtn, false, '', 'Сохранить');
        setAlert(alertBox, 'Не удалось сохранить', saveErrorText(err));
      }
    });
  }

  function openDeleteDialog(entry) {
    const quotedText = entry.text.length > 200 ? `${entry.text.slice(0, 197)}…` : entry.text;
    const confirmHtml = confirmBody({
      text: `Удалить эту запись из памяти?\n\n«${quotedText}»`,
      confirmLabel: 'Удалить',
      danger: true,
      cancelLabel: 'Отмена',
    });

    const dlg = openDialog({
      title: 'Удаление записи',
      subtitle: scopeLabel(entry),
      content: confirmHtml,
    });

    const confirmBtn = dlg.body.querySelector('[data-confirm]');
    const alertBox = dlg.body.querySelector('#cf-alert');

    confirmBtn.addEventListener('click', async () => {
      setBusy(confirmBtn, true, 'Удаляем…', 'Удалить');
      try {
        await api.deleteMemory(entry.id);
        const aIdx = (memoryData.active || []).findIndex((m) => m.id === entry.id);
        if (aIdx >= 0) memoryData.active.splice(aIdx, 1);
        const pIdx = (memoryData.proposed || []).findIndex((m) => m.id === entry.id);
        if (pIdx >= 0) memoryData.proposed.splice(pIdx, 1);
        dlg.close();
        render();
      } catch (err) {
        setBusy(confirmBtn, false, '', 'Удалить');
        setAlert(alertBox, 'Не удалось удалить', err.detail || err.message || 'Проверьте подключение и повторите попытку.');
      }
    });
  }

  await loadData();
}
