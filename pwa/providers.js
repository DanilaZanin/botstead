// providers.js: раздел «Провайдеры» (список, добавление, модели провайдера), выбор модели бота, состояния бота
// (нет модели, компьютер не запустился, перезапуск), удаление бота. API: docs/contracts.md §10–11.
// Секрет ключа уходит на сервер один раз и в интерфейсе больше не показывается: API отдаёт только has_secret и secret_tail.
// Ядро проверяет ключ до сохранения (422 key_rejected | unreachable | incompatible | invalid_base_url, 429 rate_limited);
// внутренний адрес участника уходит на одобрение администратору (status pending_admin, экран в provider-requests.js).
import * as api from './api.js';
import { ICONS, esc, plural, modelTitle, fmtAgo, fmtDateTime, alertHtml } from './ui.js';
import {
  context, field, setAlert, setBusy, clearErrors, setFieldError, failure, isServerFault, loadingHtml, stateHtml, retryButton,
  openDialog, confirmBody, wireConfirm, confirmAction, segmentedHtml, wireSegmented, segmentedValue, isDesktop,
} from './account.js';
import {
  CLI_LABEL, CLI_NOTE, VENDORS, runnerKind, kindLabel, hostPath, providerProblem, providerStatus, keyText, keyHtml, unusableReason,
  pendingProblem, modelGroups, usableModels, modelCountText, botNoModel,
} from './registry.js';
import { viewProviderLogin } from './cli-login.js';

const $ = (selector, root = document) => root.querySelector(selector);
const HASH_LIST = '#/settings/providers';
export const HASH_REQUESTS = '#/settings/provider-requests';
const isAdmin = () => context().session.user.role === 'admin';

// ---------------------------------------------------------------------------
// Отказ пробной проверки ключа (§11): 422 {error, detail} и 429. Для каждого кода свой текст у поля, к которому он
// относится. «Сохранить без проверки» (force) предлагается только там, где ключ не опровергнут: нет связи или ответ
// не похож на API моделей. Отказ ключа и запрещённый адрес силой не обходятся.
// ---------------------------------------------------------------------------
const RATE_LIMITED_TEXT = 'Слишком много проверок подряд, попробуйте через минуту';
const FORCE_NOTE = 'Провайдер сохранится со статусом «Не проверен». Боты не смогут использовать его, пока проверка не пройдёт: запустите её кнопкой «Проверить» на экране провайдера.';

// ctx: { compat: свой адрес, vendor: запись из VENDORS или undefined }.
export function verifyProblem(err, { compat = false, vendor } = {}) {
  if (err.status === 429) return { code: 'rate_limited', field: null, force: false, title: 'Проверка не запущена', text: RATE_LIMITED_TEXT, note: '' };
  if (err.status !== 422) return null;
  const changed = /изменил|повторн/i.test(String(err.detail || ''));
  switch (err.code) {
    case 'key_rejected':
      return {
        code: 'key_rejected', field: 'key', force: false, title: 'Ключ отклонён',
        note: compat ? 'Сервер не принял этот ключ' : 'Провайдер не принял этот ключ',
        text: `${compat ? 'Сервер' : 'Провайдер'} ответил, что ключ не подходит. Проверьте, что он скопирован целиком и не отозван.${vendor ? ` Новый ключ создаётся на ${vendor.keyHint}.` : ''}`,
      };
    case 'unreachable':
      return {
        code: 'unreachable', field: compat ? 'url' : null, force: true, title: 'Нет связи с провайдером',
        note: 'Этот адрес не отвечает',
        text: `Сервер botstead не дождался ответа: адрес не найден, сеть недоступна или ответ не пришёл за 30 секунд. ${compat ? 'Проверьте адрес и повторите' : 'Повторите чуть позже'}.`,
      };
    case 'incompatible':
      return {
        code: 'incompatible', field: compat ? 'url' : null, force: true,
        title: compat ? 'Адрес не похож на сервер моделей' : 'Провайдер ответил не так, как ожидалось',
        note: 'Этот адрес отвечает не как API моделей',
        text: compat
          ? 'Адрес ответил, но не как сервер моделей, совместимый с OpenAI: перенаправляет на другую страницу, отдаёт пустой список или другой формат. /v1 на конце можно не писать, а путь сервера (например /api) нужен целиком.'
          : 'Ответ пришёл, но списка моделей в нужном виде в нём нет: он пуст или в другом формате.',
      };
    case 'invalid_base_url':
      if (changed) {
        return { code: 'invalid_base_url', field: 'url', force: false, title: 'Адрес сервера изменился, нужно повторное одобрение', note: 'Адрес ждёт повторного одобрения', text: 'Имя сервера теперь указывает на другие IP-адреса. Администратор должен разрешить их заново, до этого ключ и адрес не меняются.' };
      }
      return {
        code: 'invalid_base_url', field: 'url', force: false, title: 'Адрес не принят', note: 'Адрес не принят: он запрещён или изменился',
        text: 'Адрес должен начинаться с https://, не содержать логин и пароль и не вести на закрытый сетевой адрес самого сервера. Путь после хоста допустим (например /api), но без «..», «//», «%» и пробелов. Проверьте написание.',
      };
    default:
      return { code: 'other', field: null, force: false, title: 'Проверка не прошла', note: '', text: 'Сервер отклонил запрос. Проверьте введённые значения.' };
  }
}

function verifyAlert(problem, err, unsent) {
  const detail = err.detail && err.status !== 429 ? `<details><summary class="t-footnote">Подробнее</summary><span class="t-log">${esc(err.detail)}</span></details>` : '';
  const force = problem.force
    ? `<span class="stack gap-2 force-box"><span class="banner-sub">${esc(FORCE_NOTE)}</span><button type="button" class="btn btn-secondary" data-act="force-save">Сохранить без проверки</button></span>`
    : '';
  return richAlertHtml(problem.title, `${unsent} ${problem.text}`.trim(), `${detail}${force}`);
}

// Плашка ошибки с произвольным содержимым под текстом: фокус на неё, чтобы сообщение не потерялось.
const richAlertHtml = (title, text, extraHtml = '') => `<div class="banner banner-danger" role="alert" tabindex="-1"><span class="banner-icon">${ICONS.alert}</span><span class="banner-text"><span class="banner-title">${esc(title)}</span>${text ? `<span class="banner-sub">${esc(text)}</span>` : ''}${extraHtml}</span></div>`;

// ---------------------------------------------------------------------------
// Маршрут раздела: #/settings/providers[/new | /<id> | /<id>/login]
// ---------------------------------------------------------------------------
export async function viewProviders(sub, action) {
  if (sub === 'new') return viewAdd();
  if (sub && action === 'login') return viewProviderLogin(sub);
  if (sub && !isDesktop()) return viewDetailScreen(sub);
  return viewList(sub || null);
}

async function fetchRegistry() {
  const [providers, models, bots] = await Promise.all([api.listProviders(), api.listModels(true), api.listBots().catch(() => [])]);
  return { providers, models, bots };
}

function providerIcon(p) {
  if (p.kind === 'cli_subscription') return ICONS.terminal;
  if (p.kind === 'openai_compatible') return ICONS.plug;
  return ICONS.key;
}

// Правая нижняя кнопка на телефоне: главное действие внизу экрана.
const addLink = (extra = '') => `<a class="btn btn-primary${extra}" href="${HASH_LIST}/new">${ICONS.plus}Добавить провайдера</a>`;

function loadFailure(err, what) {
  const { text } = failure(err);
  return stateHtml({ iconHtml: ICONS.cloudOff, title: `${what} не загрузились`, text: `${text} Боты продолжают работать на прежних моделях.`, actions: retryButton, kind: 'state-error' });
}

// ---------------------------------------------------------------------------
// Список провайдеров (на Mac справа панель моделей выбранного)
// ---------------------------------------------------------------------------
function providerRow(p, models, selected) {
  const status = providerStatus(p);
  const own = models.filter((m) => m.provider_id === p.id);
  const enabled = own.filter((m) => m.enabled).length;
  const count = modelCountText(enabled, own.length);
  // Ключ виден и у своего адреса: «адрес · модели · ключ», у остальных «ключ · модели».
  const sub = p.kind === 'openai_compatible' ? `${esc(`${hostPath(p.base_url)} · ${count}`)}${p.has_secret ? ` · ${keyHtml(p)}` : ''}`
    : p.kind === 'cli_subscription' ? esc(`Подписка · ${count}`) : `${keyHtml(p, { capital: true })} · ${esc(count)}`;
  return `<li><a class="list-row provider-row" href="${HASH_LIST}/${esc(p.id)}"${selected ? ' aria-current="true"' : ''}>
    <span class="row-icon" aria-hidden="true">${providerIcon(p)}</span>
    <span class="row-body"><span class="row-title" data-i18n-skip>${esc(p.name)}</span><span class="row-sub">${sub}</span>
      <span class="row-meta status-line"><span class="status-dot ${status.kind}"></span>${esc(status.text)}</span></span>
    <span class="row-chevron" aria-hidden="true">${ICONS.chevronRight}</span>
  </a></li>`;
}

function noModelBanner(bots) {
  const orphans = bots.filter(botNoModel);
  if (!orphans.length) return '';
  const names = orphans.map((b) => b.name).join(', ');
  return `<div class="banner banner-attention" role="status"><span class="banner-icon">${ICONS.alert}</span><span class="banner-text"><span class="banner-title">${orphans.length} ${plural(orphans.length, 'бот', 'бота', 'ботов')} без модели</span><span class="banner-sub">${esc(names)}: выберите модель в настройках бота или подключите провайдера.</span></span></div>`;
}

async function viewList(selectedId) {
  const c = context();
  await c.frame({
    title: 'Провайдеры',
    subtitle: 'Модели для ботов',
    backHref: '#/settings',
    body: `<div class="page-wide stack gap-4"><div class="desktop-only">${addLink(' btn-block')}</div><div class="stack gap-3" id="pv-content" aria-busy="true"></div></div>`,
    mobileActions: addLink(' btn-block'),
    desktopAside: '<div class="stack gap-3" id="pv-aside"></div>',
  });
  const content = $('#pv-content');
  const aside = $('#pv-aside');

  async function load() {
    content.setAttribute('aria-busy', 'true');
    content.innerHTML = loadingHtml('Проверяю провайдеров');
    let data;
    try { data = await fetchRegistry(); } catch (err) {
      if (err.status === 401) return;
      content.removeAttribute('aria-busy');
      content.innerHTML = loadFailure(err, 'Провайдеры');
      if (aside) aside.innerHTML = '';
      return;
    }
    content.removeAttribute('aria-busy');
    const { providers, models, bots } = data;
    if (!providers.length) {
      content.innerHTML = stateHtml({ iconHtml: ICONS.plug, title: 'Провайдеров нет', text: 'Боты не смогут отвечать, пока не подключена хотя бы одна модель: по API-ключу, через свой адрес или по подписке.' });
      if (aside) aside.innerHTML = '';
      return;
    }
    const selected = providers.find((p) => p.id === selectedId) || (aside ? providers[0] : null);
    content.innerHTML = `${noModelBanner(bots)}<ul class="list-plain">${providers.map((p) => providerRow(p, models, selected && p.id === selected.id)).join('')}</ul>`;
    if (aside && selected) {
      aside.innerHTML = detailHtml(selected, models, bots);
      wireDetail(aside, selected, models, bots);
    }
  }
  content.addEventListener('click', (e) => { if (e.target.closest('[data-act="retry"]')) load(); });
  await load();
}

// ---------------------------------------------------------------------------
// Провайдер: статус, пробный запрос, модели
// ---------------------------------------------------------------------------
async function viewDetailScreen(id) {
  const c = context();
  let data;
  try { data = await fetchRegistry(); } catch (err) {
    if (err.status === 401) return;
    await c.frame({ title: 'Провайдер', subtitle: 'Провайдеры', backHref: HASH_LIST, body: `<div class="page-narrow">${loadFailure(err, 'Данные провайдера')}</div>` });
    $('[data-act="retry"]')?.addEventListener('click', () => c.rerender());
    return;
  }
  const provider = data.providers.find((p) => p.id === id);
  if (!provider) {
    await c.frame({ title: 'Провайдер', subtitle: 'Провайдеры', backHref: HASH_LIST, body: `<div class="page-narrow">${stateHtml({ iconHtml: ICONS.plug, title: 'Провайдер не найден', text: 'Возможно, он уже удалён.', actions: `<a class="btn btn-secondary" href="${HASH_LIST}">К провайдерам</a>` })}</div>` });
    return;
  }
  await c.frame({
    title: provider.name,
    subtitle: providerSubtitle(provider),
    backHref: HASH_LIST,
    body: `<div class="page-narrow stack gap-4" id="pd-root">${detailHtml(provider, data.models, data.bots)}</div>`,
  });
  wireDetail($('#pd-root'), provider, data.models, data.bots);
}

function providerSubtitle(p, html = false) {
  const key = (q) => (html ? keyHtml(q) : keyText(q));
  const name = (t) => (html ? esc(t) : t);
  if (p.kind === 'cli_subscription') return name(`Подписка · ${CLI_LABEL[p.cli] || p.cli}`);
  if (p.kind === 'openai_compatible') return `${name(`Свой адрес · ${hostPath(p.base_url)}`)}${p.has_secret ? ` · ${key(p)}` : ''}`;
  return `${name('API-ключ')}${p.has_secret ? ` · ${key(p)}` : ''}`;
}

// Модель, которую провайдер больше не отдаёт: переключатель недоступен, массовые действия её пропускают.
const isVanished = (m) => !m.enabled && !m.manually_disabled;
// Поиск по подстроке id и названия без учёта регистра.
const modelMatches = (m, query) => {
  const q = query.trim().toLowerCase();
  return !q || m.name.toLowerCase().includes(q) || String(m.display_name || '').toLowerCase().includes(q);
};
// Сколько найденных моделей можно включить или выключить разом без подтверждения.
const BULK_CONFIRM_OVER = 50;
// Больше стольких моделей после первой проверки: подсказка, что включены все и лишние стоит выключить.
const MANY_MODELS = 30;

function modelRow(m, provider) {
  const vanished = isVanished(m);
  const parts = [];
  // Модели провайдера с ошибкой подписаны причиной («Ключ отклонён»), а не служебным именем модели.
  if (provider && ['error', 'pending_admin', 'unchecked'].includes(provider.status)) parts.push(unusableReason(provider));
  else if (modelTitle(m.name, m.display_name) !== m.name) parts.push(m.name);
  if (m.context_window) parts.push(`контекст ${m.context_window >= 1000 ? `${Math.round(m.context_window / 1000)}k` : m.context_window}`);
  if (m.manually_disabled) parts.push('отключена вами');
  else if (vanished) parts.push('провайдер больше не отдаёт эту модель');
  return `<li class="model-row"><div class="switch-row">
    <label for="mdl-${esc(m.id)}"><span class="t-body">${esc(modelTitle(m.name, m.display_name))}</span>${parts.length ? `<span class="t-footnote">${esc(parts.join(' · '))}</span>` : ''}</label>
    <input id="mdl-${esc(m.id)}" type="checkbox" role="switch" data-model="${esc(m.id)}" ${m.enabled ? 'checked' : ''} ${vanished ? 'disabled' : ''}>
  </div></li>`;
}

// Состояние «ждёт администратора» и «сохранён без проверки»: объяснение, затронутые боты и что делать дальше.
function pendingBlock(p, dependent) {
  const problem = pendingProblem(p);
  if (!problem) return '';
  const affected = dependent.length ? ` Затронуты боты: ${esc(dependent.map((b) => b.name).join(', '))}.` : '';
  const admin = isAdmin();
  let action = '';
  if (p.status === 'pending_admin') {
    action = admin
      ? `<a class="btn btn-primary" href="${HASH_REQUESTS}" data-pending-action>Открыть запросы на внутренние адреса</a>`
      : `<span class="t-footnote" data-pending-wait>Запрос отправлен. Когда администратор разрешит адрес, провайдер проверится сам.</span>`;
  }
  return `<div class="banner banner-attention" role="status" data-provider-pending="${esc(problem.code)}"><span class="banner-icon">${ICONS.alert}</span><span class="banner-text"><span class="banner-title">${esc(problem.title)}</span><span class="banner-sub">${esc(problem.text)}${affected}</span></span></div>${action ? `<div class="row gap-2 wrap-row">${action}</div>` : ''}`;
}

// Разрешённый администратором внутренний адрес: видно только администратору, с действием «Отозвать».
// key_verified === false: сервер принял запрос без ключа (список моделей публичный, у OpenRouter так), статус «ok» ничего не доказывает.
function keyUnverifiedBlock(p) {
  if (p.status !== 'ok' || p.key_verified !== false) return '';
  return `<div class="banner banner-attention" role="status" data-key-unverified><span class="banner-icon">${ICONS.alert}</span><span class="banner-text"><span class="banner-title">Ключ не проверен</span><span class="banner-sub">Сервер принял запрос без ключа: ключ не проверен. Если ключ неверный, бот получит отказ при первом запросе.</span></span></div>`;
}

function privateAllowedBlock(p) {
  if (!isAdmin() || !p.allow_private || p.status === 'pending_admin') return '';
  const ips = Array.isArray(p.allow_private_ips) && p.allow_private_ips.length ? `: ${p.allow_private_ips.join(', ')}` : '';
  return `<div class="banner banner-info" role="status" data-private-allowed><span class="banner-icon">${ICONS.lock}</span><span class="banner-text"><span class="banner-title">Разрешён внутренний адрес${esc(ips)}</span><span class="banner-sub">Сервер может обращаться к этим адресам во внутренней сети. Отзыв вернёт провайдера в ожидание одобрения.</span></span></div>
    <div class="row gap-2"><button type="button" class="btn btn-secondary" data-act="revoke-private">Отозвать</button></div>`;
}

function detailHtml(p, allModels, bots) {
  const models = allModels.filter((m) => m.provider_id === p.id);
  const enabled = models.filter((m) => m.enabled).length;
  const status = providerStatus(p, { ago: false });
  const cli = p.kind === 'cli_subscription';
  const problem = status.problem;
  const loginHref = `${HASH_LIST}/${esc(p.id)}/login`;
  const dependent = bots.filter((b) => b.provider_id === p.id);
  const checkIdle = `${ICONS.retry}Проверить`;
  const pending = p.status === 'pending_admin';
  let recovery = '';
  if (p.status === 'error' && problem) {
    const action = cli ? `<a class="btn btn-primary" href="${loginHref}">Войти</a>`
      : `<button type="button" class="btn btn-secondary" data-act="replace">${p.kind === 'openai_compatible' ? 'Изменить адрес или ключ' : 'Заменить ключ'}</button>`;
    recovery = `<div class="banner ${problem.code === 'login' ? 'banner-attention' : 'banner-danger'}" role="status"><span class="banner-icon">${ICONS.alert}</span><span class="banner-text"><span class="banner-title">${esc(problem.title)}</span><span class="banner-sub">${esc(problem.text)}${dependent.length ? ` Затронуты боты: ${esc(dependent.map((b) => b.name).join(', '))}.` : ''}</span>${p.last_error ? `<details><summary class="t-footnote">Подробнее</summary><span class="t-log">${esc(p.last_error)}</span></details>` : ''}</span></div>${action ? `<div class="row gap-2">${action}</div>` : ''}`;
  } else if (pending || p.status === 'unchecked') {
    recovery = pendingBlock(p, dependent);
  } else if (cli && p.status !== 'ok') {
    recovery = `<div class="banner banner-attention" role="status"><span class="banner-icon">${ICONS.alert}</span><span class="banner-text"><span class="banner-title">Нужен вход</span><span class="banner-sub">Войдите в аккаунт подписки: откроется окно входа.</span></span></div><div class="row gap-2"><a class="btn btn-primary" href="${loginHref}">Войти</a></div>`;
  }
  const modelTools = `<div class="stack gap-2" data-model-tools>
      <input id="pd-search" class="input" type="search" placeholder="Поиск модели" aria-label="Поиск модели" autocomplete="off" autocapitalize="none" autocorrect="off" spellcheck="false">
      <div class="row gap-2 wrap-row"><button type="button" class="btn btn-secondary" data-act="models-enable">Включить все</button><button type="button" class="btn btn-secondary" data-act="models-disable">Выключить все</button></div>
      <p class="t-footnote" id="pd-filter-note" hidden></p>
      <p class="t-footnote" id="pd-bulk" role="status" aria-live="polite" hidden></p>
    </div>`;
  const modelsBlock = models.length
    ? `${modelTools}<ul class="settings-group list-plain" id="pd-models">${models.map((m) => modelRow(m, p)).join('')}</ul>
       <p class="t-footnote">Включённые модели можно выбрать в настройках бота.${p.status === 'ok' ? '' : ' Пока провайдер не в порядке, выбрать их нельзя.'}</p>`
    : stateHtml({ iconHtml: ICONS.plug, title: 'Провайдер не вернул модели', text: cli && p.status !== 'ok' ? 'Список появится после входа.' : pending ? 'Список появится после одобрения администратора.' : 'Список пуст. Запросите его заново: кнопка «Проверить» выше.' });
  return `<div class="stack gap-4" data-provider="${esc(p.id)}">
    <div class="row gap-3 provider-head">
      <span class="row-icon desktop-only" aria-hidden="true">${providerIcon(p)}</span>
      <span class="row-body desktop-only"><span class="t-headline" data-i18n-skip>${esc(p.name)}</span><span class="t-footnote wrap">${providerSubtitle(p, true)}</span></span>
      <button type="button" class="icon-btn sunken provider-menu-btn" data-act="menu" aria-label="Действия с провайдером" aria-haspopup="dialog">${ICONS.more}</button>
    </div>
    <div class="form-alert" id="pd-alert" aria-live="polite"></div>
    <section class="card card-pad stack gap-3" aria-labelledby="pd-check-h">
      <div class="row gap-3">
        <span class="stack gap-1 flex-1 min-w-0"><span class="t-headline" id="pd-check-h" style="font-size:15px;">Проверка</span>${p.last_check_at ? `<span class="t-footnote">${esc(fmtAgo(p.last_check_at))}</span>` : '<span class="t-footnote">Ещё не проверялся</span>'}<span class="status-line t-footnote" style="color:var(--fg-default);"><span class="status-dot ${status.kind}"></span>${esc(status.text)}</span></span>
        ${pending ? '' : `<button type="button" class="btn btn-secondary" data-act="check">${checkIdle}</button>`}
      </div>
      ${recovery}
      ${keyUnverifiedBlock(p)}
      ${privateAllowedBlock(p)}
    </section>
    ${cli && p.status === 'ok' ? `<a class="btn btn-secondary" href="${loginHref}">Войти заново</a>` : ''}
    <h2 class="section-label" id="pd-models-h">Модели · включено ${enabled} из ${models.length}</h2>
    ${modelsBlock}
    <button type="button" class="btn btn-danger btn-block" data-act="delete">${ICONS.trash}Удалить провайдера</button>
  </div>`;
}

function showNote(box, title, text) {
  box.innerHTML = alertHtml(title, text);
}

function wireDetail(root, p, allModels, bots) {
  const c = context();
  const box = $('#pd-alert', root);
  const reload = () => c.rerender();
  const list = $('#pd-models', root);
  const search = $('#pd-search', root);
  const filterNote = $('#pd-filter-note', root);
  const bulkNote = $('#pd-bulk', root);
  const own = () => allModels.filter((m) => m.provider_id === p.id);
  const shown = () => own().filter((m) => modelMatches(m, search ? search.value : ''));
  let bulkBusy = false;

  function paintHeader() {
    const models = own();
    $('#pd-models-h', root).textContent = `Модели · включено ${models.filter((m) => m.enabled).length} из ${models.length}`;
  }
  // Список по строке поиска: скрытые модели остаются в allModels, переключатели видимых рисуются заново.
  function paintList() {
    if (!list) return;
    const rows = shown();
    list.innerHTML = rows.length ? rows.map((m) => modelRow(m, p)).join('') : `<li class="model-row"><span class="t-footnote">Ничего не найдено</span></li>`;
    const total = own().length;
    const filtering = search.value.trim() !== '';
    filterNote.hidden = !filtering;
    filterNote.textContent = filtering ? `Найдено ${rows.length} из ${total}. «Включить все» и «Выключить все» действуют на найденные модели.` : '';
  }
  if (search) search.addEventListener('input', paintList);

  // Пачка запросов по одной модели: массового PATCH в ядре нет (/api/models/refresh только перепроверяет провайдеров).
  async function runBulk(want, rows) {
    const targets = rows.filter((m) => m.enabled !== want && !isVanished(m));
    if (!targets.length) {
      bulkNote.hidden = false;
      bulkNote.textContent = want ? 'Все найденные модели уже включены.' : 'Все найденные модели уже выключены.';
      return;
    }
    bulkBusy = true;
    box.innerHTML = '';
    const controls = root.querySelectorAll('[data-model-tools] button, [data-model-tools] input');
    controls.forEach((el) => { el.disabled = true; });
    list.inert = true;
    list.setAttribute('aria-busy', 'true');
    bulkNote.hidden = false;
    const total = targets.length;
    let done = 0;
    let failed = null;
    for (const m of targets) {
      if (!root.isConnected) return;
      bulkNote.textContent = want ? `Включаю: ${done} из ${total}` : `Выключаю: ${done} из ${total}`;
      try { Object.assign(m, await api.patchModel(m.id, { enabled: want })); done += 1; } catch (err) { failed = err; break; }
    }
    if (!root.isConnected) return;
    bulkBusy = false;
    controls.forEach((el) => { el.disabled = false; });
    list.inert = false;
    list.removeAttribute('aria-busy');
    paintList();
    paintHeader();
    if (failed) {
      bulkNote.textContent = '';
      bulkNote.hidden = true;
      showNote(box, 'Изменены не все модели', failure(failed, `Изменено ${done} из ${total}. `).text);
      box.firstElementChild?.focus?.();
    } else {
      bulkNote.textContent = want ? `Включено моделей: ${done}` : `Выключено моделей: ${done}`;
    }
  }
  function askBulk(want) {
    if (bulkBusy) return;
    const rows = shown();
    if (!rows.length) return;
    if (rows.length <= BULK_CONFIRM_OVER) { runBulk(want, rows); return; }
    const count = rows.length;
    confirmAction({
      title: want ? 'Включить все найденные модели?' : 'Выключить все найденные модели?',
      subtitle: p.name,
      text: `Найдено моделей: ${count}. Переключатели изменятся у всех найденных, сузить выбор можно поиском.`,
      confirmLabel: want ? 'Включить' : 'Выключить',
      danger: false,
      run: async () => {},
      done: () => runBulk(want, rows),
    });
  }

  root.addEventListener('change', async (e) => {
    const input = e.target.closest('input[data-model]');
    if (!input || bulkBusy) return;
    const id = input.getAttribute('data-model');
    const model = allModels.find((m) => m.id === id);
    const want = input.checked;
    box.innerHTML = '';
    input.disabled = true;
    try {
      const saved = await api.patchModel(id, { enabled: want });
      Object.assign(model, saved);
      // Строка перерисовывается целиком: пометка «отключена вами» должна соответствовать новому состоянию.
      input.closest('.model-row').outerHTML = modelRow(model, p);
      document.getElementById(`mdl-${id}`)?.focus();
    } catch (err) {
      input.checked = !want;
      input.disabled = false;
      showNote(box, 'Модель не изменена', failure(err).text);
    }
    paintHeader();
  });

  root.addEventListener('click', async (e) => {
    const act = e.target.closest('[data-act]');
    if (!act) return;
    const name = act.getAttribute('data-act');
    if (name === 'check') {
      box.innerHTML = '';
      setBusy(act, true, 'Проверяю', '');
      try { await api.checkProvider(p.id); } catch (err) {
        setBusy(act, false, '', `${ICONS.retry}Проверить`);
        setAlert(box, 'Проверка не выполнена', failure(err).text);
        return;
      }
      reload();
    } else if (name === 'menu') openMenu(p, bots, reload);
    else if (name === 'replace') openCredentialsDialog(p, reload);
    else if (name === 'delete') confirmDeleteProvider(p, bots);
    else if (name === 'revoke-private') confirmRevokePrivate(p, bots, reload);
    else if (name === 'retry') reload();
    else if (name === 'models-enable') askBulk(true);
    else if (name === 'models-disable') askBulk(false);
  });
}

// Меню действий: шаги в том же диалоге, как у пользователей.
function openMenu(p, bots, reload) {
  const cli = p.kind === 'cli_subscription';
  const compat = p.kind === 'openai_compatible';
  const dlg = openDialog({
    title: p.name,
    subtitle: providerSubtitle(p),
    content: `<div class="stack gap-2">
      <button type="button" class="btn btn-secondary btn-block" data-do="rename">Переименовать</button>
      ${cli ? `<a class="btn btn-secondary btn-block" href="${HASH_LIST}/${esc(p.id)}/login" data-close>Войти заново</a>`
      : `<button type="button" class="btn btn-secondary btn-block" data-do="credentials">${compat ? 'Изменить адрес или ключ' : 'Заменить ключ'}</button>`}
      <button type="button" class="btn btn-danger btn-block" data-do="delete">Удалить провайдера</button>
    </div>`,
  });
  dlg.body.addEventListener('click', (e) => {
    const act = e.target.closest('[data-do]');
    if (!act) return;
    const name = act.getAttribute('data-do');
    dlg.close();
    if (name === 'rename') openRenameDialog(p, reload);
    else if (name === 'credentials') openCredentialsDialog(p, reload);
    else if (name === 'delete') confirmDeleteProvider(p, bots);
  });
}

function patchProblem(err) {
  if (err.status === 409) return { title: 'Название занято', text: 'Провайдер с таким названием уже есть.' };
  if (err.status === 400) return { title: 'Изменение не принято', text: 'Проверьте значения полей.' };
  if (err.status === 404) return { title: 'Провайдер не найден', text: 'Возможно, он уже удалён.' };
  return failure(err, 'Изменения не сохранены. ');
}

function openRenameDialog(p, reload) {
  const idle = 'Сохранить';
  const dlg = openDialog({
    title: 'Название провайдера',
    content: `<form class="stack gap-4" id="rn-form" novalidate>
      ${field({ id: 'rn-name', label: 'Название', value: p.name })}
      <div class="form-alert" id="rn-alert"></div>
      <div class="btn-row btn-row-2"><button type="button" class="btn btn-secondary" data-close>Отмена</button><button type="submit" class="btn btn-primary" id="rn-submit">${idle}</button></div>
    </form>`,
  });
  const form = $('#rn-form', dlg.el);
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    clearErrors(form);
    const value = $('#rn-name', form).value.trim();
    if (!value) { setFieldError($('#rn-name', form), 'Введите название'); return; }
    const submit = $('#rn-submit', form);
    setBusy(submit, true, 'Сохраняю', idle);
    try { await api.patchProvider(p.id, { name: value }); } catch (err) {
      setBusy(submit, false, '', idle);
      const { title, text } = patchProblem(err);
      setAlert($('#rn-alert', form), title, text);
      return;
    }
    dlg.close();
    reload();
  });
}

// Замена ключа и смена адреса. Ядро само проверяет новый ключ до сохранения (§11): при отказе прежний ключ и адрес
// остаются, диалог не закрывается, введённое не теряется. Смена адреса требует ключ в том же запросе: этот случай
// проверяем до отправки. После записи повторной проверки нет, поэтому /check здесь не вызывается.
function openCredentialsDialog(p, reload) {
  const compat = p.kind === 'openai_compatible';
  const vendor = VENDORS.find((v) => v.kind === p.kind);
  const idle = 'Сохранить и проверить';
  const dlg = openDialog({
    title: compat ? 'Адрес и ключ' : 'Заменить ключ',
    subtitle: p.name,
    content: `<form class="stack gap-4" id="cr-form" novalidate autocomplete="off">
      ${p.has_secret ? `<div class="banner banner-info" role="note" id="cr-warn"><span class="banner-icon">${ICONS.lock}</span><span class="banner-text"><span class="banner-title">Прежний ключ остаётся, пока новый не пройдёт проверку</span><span class="banner-sub">Сервер сначала пробует новый ключ у провайдера и только потом заменяет прежний.</span></span></div>` : ''}
      ${compat ? field({ id: 'cr-url', label: 'Адрес сервера моделей', value: p.base_url || '', inputmode: 'url', hint: 'Если сменить адрес, ключ нужно ввести заново.' }) : ''}
      ${field({ id: 'cr-key', label: compat ? 'API-ключ' : 'Новый API-ключ', type: 'password', autocomplete: 'new-password', placeholder: vendor ? vendor.placeholder : '', hint: p.has_secret ? `Сейчас: ${keyText(p)}. Показать его нельзя.` : 'Хранится на сервере зашифрованным.', mono: true })}
      <div class="form-alert" id="cr-alert"></div>
      <div class="btn-row btn-row-2"><button type="button" class="btn btn-secondary" data-close>Отмена</button><button type="submit" class="btn btn-primary" id="cr-submit">${idle}</button></div>
    </form>`,
  });
  const form = $('#cr-form', dlg.el);
  const key = $('#cr-key', form);
  const urlInput = compat ? $('#cr-url', form) : null;
  const alertBox = $('#cr-alert', form);
  const submit = $('#cr-submit', form);
  key.setAttribute('data-1p-ignore', '');
  key.setAttribute('data-lpignore', 'true');
  let changed = false; // на сервере что-то изменилось: после закрытия список и статус перечитываются
  let inFlight = false;
  let lastBody = null;
  dlg.onClose = () => { if (changed) reload(); };

  function lock(locked) {
    form.querySelectorAll('input, button').forEach((el) => { if (!el.hasAttribute('data-close')) el.disabled = locked; });
  }
  function showAlert(html) {
    alertBox.innerHTML = html;
    alertBox.firstElementChild.focus();
  }
  function fail(err) {
    const problem = verifyProblem(err, { compat, vendor });
    if (problem) {
      const target = problem.field === 'key' ? key : problem.field === 'url' ? urlInput : null;
      if (target && problem.note) setFieldError(target, problem.note);
      showAlert(verifyAlert(problem, err, 'Изменения не сохранены, прежний ключ и адрес на месте.'));
      return;
    }
    if (err.status === 409) {
      changed = true;
      setAlert(alertBox, 'Провайдер изменился во время проверки', 'Закройте окно: экран обновится. Затем повторите замену. Введённый ключ нигде не сохранён.');
      return;
    }
    if (err.status === 404) { setAlert(alertBox, 'Провайдер не найден', 'Возможно, он уже удалён.'); return; }
    if (err.status === 400) { setAlert(alertBox, 'Изменение не принято', 'Проверьте значения полей. Прежний ключ не изменён.'); return; }
    const { title, text } = failure(err, 'Изменения не сохранены. ');
    setAlert(alertBox, title, `${text} Прежний ключ не изменён.`);
  }

  async function send(body, forced) {
    if (inFlight) return;
    inFlight = true;
    lastBody = body;
    const forceBtn = forced ? $('[data-act="force-save"]', alertBox) : null;
    if (!forced) clearErrors(form);
    lock(true);
    if (forceBtn) setBusy(forceBtn, true, 'Сохраняю без проверки', '');
    else setBusy(submit, true, 'Проверяем ключ', idle);
    try {
      await api.patchProvider(p.id, body);
    } catch (err) {
      inFlight = false;
      lock(false);
      setBusy(submit, false, '', idle);
      if (forced) clearErrors(form);
      fail(err);
      return;
    }
    inFlight = false;
    changed = true;
    key.value = ''; // ключ ушёл на сервер: поле очищается
    dlg.close();
  }

  alertBox.addEventListener('click', (e) => {
    if (e.target.closest('[data-act="force-save"]') && lastBody) send({ ...lastBody, force: true }, true);
  });
  // Правка полей делает прежний отказ неактуальным: убираем его вместе с действием «Сохранить без проверки».
  form.addEventListener('input', () => { if (alertBox.firstElementChild) clearErrors(form); lastBody = null; });

  form.addEventListener('submit', (e) => {
    e.preventDefault();
    if (inFlight) return;
    clearErrors(form);
    const url = compat ? urlInput.value.trim() : '';
    const changedUrl = compat && url !== (p.base_url || '');
    if (compat && !url) { setFieldError(urlInput, 'Введите адрес'); return; }
    if (!key.value) { setFieldError(key, changedUrl ? 'При смене адреса ключ нужно ввести заново' : 'Введите ключ'); key.focus(); return; }
    const body = { secret: key.value };
    if (changedUrl) body.base_url = url;
    send(body, false);
  });
}

// Отзыв разрешения на внутренний адрес (только администратор): провайдер вернётся в ожидание одобрения.
function confirmRevokePrivate(p, bots, reload) {
  const dependent = bots.filter((b) => b.provider_id === p.id);
  const bound = dependent.length
    ? `Боты на моделях этого провайдера перестанут отвечать: ${dependent.map((b) => b.name).join(', ')}.`
    : 'Ботов с моделью этого провайдера нет.';
  confirmAction({
    title: 'Отозвать разрешение?',
    subtitle: p.name,
    text: `Сервер перестанет ходить на внутренний адрес, провайдер перейдёт в «Ждёт одобрения администратора». ${bound} Разрешить адрес снова можно в запросах на внутренние адреса.`,
    confirmLabel: 'Отозвать',
    run: () => api.allowPrivateProvider(p.id, { allow: false }),
    describeError: (err) => (err.status === 404 ? { title: 'Провайдер уже удалён', text: 'Обновите список.' } : err.status === 403 ? { title: 'Нужны права администратора', text: 'Разрешение не отозвано.' } : failure(err, 'Разрешение не отозвано. ')),
    done: reload,
  });
}

function confirmDeleteProvider(p, bots) {
  const dependent = bots.filter((b) => b.provider_id === p.id);
  const bound = dependent.length
    ? `Без модели останутся ${dependent.length} ${plural(dependent.length, 'бот', 'бота', 'ботов')}: ${dependent.map((b) => b.name).join(', ')}. Им нужно будет выбрать другую модель.`
    : 'Ботов с моделью этого провайдера нет.';
  confirmAction({
    title: 'Удалить провайдера?',
    subtitle: p.name,
    text: `${p.kind === 'cli_subscription' ? 'Подключение и список моделей удалятся.' : 'Ключ на сервере и список моделей удалятся.'} ${bound}`,
    confirmLabel: 'Удалить',
    run: () => api.deleteProvider(p.id),
    describeError: (err) => (err.status === 404 ? { title: 'Провайдер уже удалён', text: 'Обновите список.' } : failure(err, 'Провайдер не удалён. ')),
    done: () => {
      if (location.hash === HASH_LIST) context().rerender();
      else location.hash = HASH_LIST;
    },
  });
}

// ---------------------------------------------------------------------------
// Добавление: API-ключ, свой адрес, подписка
// ---------------------------------------------------------------------------
// Каждый способ описан двумя строками: что нужно и как считается оплата. Подробное сравнение открывает «Какой выбрать».
const KIND_OPTIONS = [
  { value: 'api', label: 'API-ключ', need: 'Нужен ключ из личного кабинета Anthropic, OpenAI или Google.', pay: 'Оплата по токенам: счёт выставляет сам сервис.' },
  { value: 'endpoint', label: 'Свой адрес', need: 'Нужны адрес сервера моделей, совместимого с OpenAI, и ключ к нему.', pay: 'Оплата зависит от сервера: у своего сервера платите за его работу.' },
  { value: 'cli', label: 'Подписка', need: 'Нужен аккаунт Claude, ChatGPT или Google с платным тарифом.', pay: 'Отдельной оплаты нет: расход идёт в лимит тарифа.' },
];

function kindChoiceHtml() {
  return `<div role="radiogroup" aria-label="Вид провайдера" data-seg="Вид провайдера" class="stack gap-2">${KIND_OPTIONS.map((k, i) => `<button type="button" role="radio" class="model-option kind-option" data-value="${k.value}" aria-checked="${i === 0}" tabindex="${i === 0 ? 0 : -1}"><span class="radio-dot" aria-hidden="true"></span><span class="model-option-text"><span class="model-option-title">${esc(k.label)}</span><span class="model-option-sub">${esc(k.need)}</span><span class="model-option-sub">${esc(k.pay)}</span></span></button>`).join('')}</div>
    <button type="button" class="link-btn" data-act="compare" aria-haspopup="dialog">Какой выбрать</button>`;
}

function openCompareDialog() {
  const rows = [
    { title: 'API-ключ', need: 'Ключ из личного кабинета Anthropic, OpenAI или Google.', pay: 'По токенам: счёт выставляет сам сервис, расход виден в разделе «Расход».', fit: 'Нужен предсказуемый расход и не нужна подписка.' },
    { title: 'Свой адрес', need: 'Адрес сервера моделей, совместимого с OpenAI (например Ollama), и ключ к нему.', pay: 'Зависит от сервера: у своего сервера платите за его работу, у чужого по его тарифу.', fit: 'Свои или локальные модели.' },
    { title: 'Подписка', need: 'Аккаунт Claude, ChatGPT или Google с платным тарифом.', pay: 'Отдельной оплаты нет: расход идёт в лимит тарифа и может закончиться раньше срока.', fit: 'Подписка уже есть, платить ещё и за токены не хочется.' },
  ];
  openDialog({
    title: 'Какой способ выбрать',
    subtitle: 'Сравнение трёх способов подключения',
    content: `<div class="stack gap-3">${rows.map((r) => `<section class="card card-pad stack gap-1" aria-label="${esc(r.title)}">
        <h3 class="t-callout compare-title">${esc(r.title)}</h3>
        <dl class="compare-list"><dt>Что нужно</dt><dd>${esc(r.need)}</dd><dt>Как считается оплата</dt><dd>${esc(r.pay)}</dd><dt>Когда подходит</dt><dd>${esc(r.fit)}</dd></dl>
      </section>`).join('')}
      <div class="banner banner-info" role="note"><span class="banner-icon">${ICONS.lock}</span><span class="banner-text"><span class="banner-title">Как устроен вход по подписке</span><span class="banner-sub">Пароль от аккаунта в botstead не вводится: вы входите на странице самого сервиса и вставляете полученный код. Данные входа хранятся на сервере отдельно для каждого пользователя и доступны только его ботам. Содержимое окна входа нигде не сохраняется.</span></span></div>
      <button type="button" class="btn btn-primary btn-block" data-close>Понятно</button></div>`,
    focus: 'dialog',
  });
}

async function viewAdd() {
  const c = context();
  const admin = isAdmin();
  const existing = await api.listProviders().catch(() => []);
  const submitIdle = 'Проверить и сохранить';
  const submitBtn = (extra = '') => `<button type="submit" form="pa-form" class="btn btn-primary btn-block" data-pa-submit${extra}>${submitIdle}</button>`;
  const pasteBtn = (target, label) => `<button type="button" class="icon-btn sunken" data-paste="${target}" aria-label="${label}">${ICONS.clipboard}</button>`;
  const privateNotice = admin
    ? 'Адреса из локальной сети (например 192.168.1.5:11434) по умолчанию закрыты, чтобы бот не добрался до домашних устройств. Если указать такой адрес, провайдер встанет в ожидание, а разрешить его можно в настройках, в запросах на внутренние адреса.'
    : 'Адреса из локальной сети (например 192.168.1.5:11434) по умолчанию закрыты, чтобы бот не добрался до домашних устройств. Если указать такой адрес, запрос уйдёт администратору, а провайдер заработает после его одобрения.';
  await c.frame({
    title: 'Новый провайдер',
    subtitle: 'Подключение и пробный запрос',
    backHref: HASH_LIST,
    body: `<form id="pa-form" class="page-narrow stack gap-4" novalidate autocomplete="off">
      <div class="stack gap-2"><span class="t-footnote">Вид провайдера</span>${kindChoiceHtml()}</div>
      <section class="stack gap-4" data-panel="api">
        <div class="stack gap-2"><span class="t-footnote">Сервис</span>${segmentedHtml('Сервис', VENDORS.map((v) => ({ value: v.kind, label: v.label })), 'anthropic_api')}</div>
        ${field({ id: 'pa-key', label: 'API-ключ', type: 'password', autocomplete: 'new-password', placeholder: VENDORS[0].placeholder, hint: 'Хранится на сервере зашифрованным. К боту не попадает.', mono: true, after: pasteBtn('pa-key', 'Вставить ключ из буфера') })}
        ${field({ id: 'pa-name', label: 'Название', value: VENDORS[0].name })}
        <p class="t-footnote" id="pa-google" hidden>Google API можно подключить и проверить, но привязать к боту пока нельзя: ядро не умеет выбирать для него модель.</p>
      </section>
      <section class="stack gap-4" data-panel="endpoint" hidden>
        ${field({ id: 'pa-ep-name', label: 'Название', value: 'Свой адрес' })}
        ${field({ id: 'pa-url', label: 'Адрес сервера моделей', inputmode: 'url', placeholder: 'https://models.example.org/v1', hint: 'Адрес сервера моделей, совместимого с OpenAI. /v1 на конце можно не писать; путь сервера (например /api) нужен.' })}
        ${field({ id: 'pa-ep-key', label: 'API-ключ', type: 'password', autocomplete: 'new-password', mono: true, hint: 'Нужен всегда. Если сервер ключ не проверяет (например, Ollama), введите любую строку.', after: pasteBtn('pa-ep-key', 'Вставить ключ из буфера') })}
        <div class="banner banner-info" role="note"><span class="banner-icon">${ICONS.lock}</span><span class="banner-text" id="pa-private"><span class="banner-title">Закрытые адреса</span><span class="banner-sub">${esc(privateNotice)}</span></span></div>
      </section>
      <section class="stack gap-4" data-panel="cli" hidden>
        <div role="radiogroup" aria-label="Подписка" class="stack gap-2" id="pa-cli-group">
          ${['claude', 'codex', 'agy'].map((cli, i) => `<button type="button" role="radio" class="model-option" data-cli="${cli}" aria-checked="${i === 0}" tabindex="${i === 0 ? 0 : -1}"><span class="radio-dot" aria-hidden="true"></span><span class="model-option-text"><span class="model-option-title">${esc(CLI_LABEL[cli])}</span><span class="model-option-sub">${esc(CLI_NOTE[cli])}</span></span></button>`).join('')}
        </div>
        <div class="banner banner-info" role="note"><span class="banner-icon">${ICONS.terminal}</span><span class="banner-text"><span class="banner-title">Откроется окно входа</span><span class="banner-sub">Вход в аккаунт проходит на странице самого сервиса, пароль в botstead не вводится. Данные входа хранятся отдельно для каждого пользователя и доступны только его ботам.</span></span></div>
      </section>
      <div id="pa-probe" role="status" aria-live="polite"></div>
      <div id="pa-fail"></div>
      <div class="desktop-only">${submitBtn()}</div>
    </form>`,
    mobileActions: submitBtn(),
  });

  const form = $('#pa-form');
  const submits = c.app.querySelectorAll('[data-pa-submit]');
  const kindGroup = $('[role="radiogroup"][data-seg="Вид провайдера"]', form);
  wireSegmented(form);
  for (const id of ['pa-key', 'pa-ep-key']) {
    const input = $(`#${id}`, form);
    input.setAttribute('data-1p-ignore', '');
    input.setAttribute('data-lpignore', 'true');
  }
  let kind = 'api';
  let nameTouched = false;
  let inFlight = false;
  let lastBody = null; // последнее отправленное тело: «Сохранить без проверки» повторяет его с force: true
  const vendor = () => VENDORS.find((v) => v.kind === segmentedValue(form, 'Сервис')) || VENDORS[0];
  const probe = $('#pa-probe');
  const failBox = $('#pa-fail');

  function setSubmitLabel() {
    const label = kind === 'cli' ? 'Открыть терминал входа' : submitIdle;
    submits.forEach((b) => { b.textContent = label; });
  }
  function reset() {
    clearErrors(form);
    probe.innerHTML = '';
    failBox.innerHTML = '';
  }
  kindGroup.addEventListener('click', (e) => {
    const radio = e.target.closest('[role="radio"]');
    if (!radio) return;
    kind = radio.getAttribute('data-value');
    form.querySelectorAll('[data-panel]').forEach((panel) => { panel.hidden = panel.getAttribute('data-panel') !== kind; });
    reset();
    lastBody = null;
    setSubmitLabel();
  });
  $('[role="radiogroup"][data-seg="Сервис"]', form).addEventListener('click', () => {
    const v = vendor();
    $('#pa-key', form).placeholder = v.placeholder;
    $('#pa-google', form).hidden = v.kind !== 'google_api';
    if (!nameTouched) $('#pa-name', form).value = v.name;
    reset();
    lastBody = null;
  });
  $('#pa-name', form).addEventListener('input', () => { nameTouched = true; });
  // Правка полей делает прежний отказ неактуальным: убираем его вместе с действием «Сохранить без проверки».
  form.addEventListener('input', () => {
    if (failBox.firstElementChild || probe.firstElementChild) reset();
    lastBody = null;
  });
  wireRadioGroup($('#pa-cli-group', form));
  form.addEventListener('click', async (e) => {
    if (e.target.closest('[data-act="compare"]')) { openCompareDialog(); return; }
    if (e.target.closest('[data-act="force-save"]')) { if (lastBody) send({ ...lastBody, force: true }, true); return; }
    const paste = e.target.closest('[data-paste]');
    if (!paste) return;
    const input = document.getElementById(paste.getAttribute('data-paste'));
    try {
      input.value = (await navigator.clipboard.readText()).trim();
      input.focus();
      input.dispatchEvent(new Event('input', { bubbles: true }));
    } catch {
      setFieldError(input, 'Не удалось прочитать буфер: вставьте ключ вручную');
    }
  });

  // Пока идёт проверка, кнопка в ожидании («Проверяем ключ») и повторная отправка невозможна.
  function busyLabel(forced) {
    if (kind === 'cli') return 'Проверяю';
    return forced ? 'Сохраняем без проверки' : 'Проверяем ключ';
  }
  function setBusyAll(busy, forced = false) {
    submits.forEach((b) => setBusy(b, busy, busyLabel(forced), kind === 'cli' ? 'Открыть терминал входа' : submitIdle));
    form.querySelectorAll('input').forEach((i) => { i.readOnly = busy; });
    form.querySelectorAll('[data-act="force-save"]').forEach((b) => { b.disabled = busy; });
  }

  async function modelsFound(id) {
    try { return (await api.listModels(true)).filter((m) => m.provider_id === id && m.enabled).length; } catch { return null; }
  }

  // Итоговый экран: поля формы вместе с введённым ключом убираются из разметки.
  function showResult({ tone, title, text, hint = '', link, linkLabel }) {
    const attention = tone === 'attention';
    form.innerHTML = `<div class="state-box ${attention ? '' : 'state-ok'}" role="status">
      <span class="state-icon" aria-hidden="true" style="color:var(${attention ? '--attention-text' : '--success-fg'});">${attention ? ICONS.alert : ICONS.check}</span>
      <h2 class="state-title">${esc(title)}</h2>
      <p class="t-footnote state-text">${esc(text)}</p>
      ${hint ? `<p class="t-footnote state-text" data-many-models>${esc(hint)}</p>` : ''}
      <div class="state-actions desktop-only"><a class="btn btn-primary" href="${link}">${esc(linkLabel)}</a></div>
    </div>`;
    const bar = c.app.querySelector('.action-bar');
    if (bar) bar.innerHTML = `<a class="btn btn-primary btn-block" href="${link}">${esc(linkLabel)}</a>`;
  }
  function showSuccess(created, count, googleNote) {
    showResult({
      tone: 'ok', title: 'Провайдер отвечает',
      text: `${created.name}${count === null ? '' : `. Найдено моделей: ${count}`}.${googleNote ? ' Привязать Google API к боту пока нельзя.' : ''}`,
      hint: count > MANY_MODELS ? `Включены все ${count}. Выключите лишние на экране провайдера: поиск и кнопка «Выключить все».` : '',
      link: `${HASH_LIST}/${esc(created.id)}`, linkLabel: 'Выбрать модели',
    });
  }
  // Ответ 201 без пробного запроса: ждёт администратора или сохранён без проверки.
  function showSavedWithoutProbe(created) {
    const problem = pendingProblem(created);
    if (created.status === 'pending_admin') {
      showResult({
        tone: 'attention', title: 'Ждёт одобрения администратора',
        text: `${created.name}. Адрес находится во внутренней сети. Администратор должен разрешить его вручную. Запрос отправлен, боты смогут пользоваться провайдером после одобрения.`,
        link: `${HASH_LIST}/${esc(created.id)}`, linkLabel: 'Открыть провайдера',
      });
      return;
    }
    showResult({
      tone: 'attention', title: problem ? problem.title : 'Провайдер сохранён',
      text: `${created.name}. ${problem ? problem.text : 'Проверьте его на экране провайдера.'}`,
      link: `${HASH_LIST}/${esc(created.id)}`, linkLabel: 'Открыть провайдера',
    });
  }

  function fail(err) {
    const compat = kind === 'endpoint';
    const problem = verifyProblem(err, { compat, vendor: kind === 'api' ? vendor() : undefined });
    if (problem) {
      const target = problem.field === 'key' ? (compat ? $('#pa-ep-key', form) : $('#pa-key', form)) : problem.field === 'url' ? $('#pa-url', form) : null;
      if (target && problem.note) setFieldError(target, problem.note);
      failBox.innerHTML = verifyAlert(problem, err, 'Провайдер не сохранён.');
      failBox.firstElementChild.focus();
      submits.forEach((b) => { b.innerHTML = `${ICONS.retry}Проверить ещё раз`; });
      return;
    }
    if (err.status === 409) {
      const target = compat ? $('#pa-ep-name', form) : $('#pa-name', form);
      setFieldError(target, 'Провайдер с таким названием уже есть');
      target.focus();
    } else if (err.status === 403) setAlert(failBox, 'Нужны права администратора', 'Провайдер не сохранён: это действие доступно только администратору.');
    else if (err.status === 400) setAlert(failBox, 'Данные не приняты', 'Проверьте поля формы. Провайдер не сохранён.');
    else if (isServerFault(err)) setAlert(failBox, 'Ошибка сервера при сохранении', failure(err).text);
    else setAlert(failBox, failure(err).title, `Провайдер не сохранён. ${failure(err).text}`);
  }

  async function send(body, forced = false) {
    if (inFlight) return;
    inFlight = true;
    lastBody = body;
    if (!forced) reset();
    else clearErrors(form);
    setBusyAll(true, forced);
    probe.innerHTML = `<div class="banner banner-info"><span class="banner-icon spin" aria-hidden="true">${ICONS.spinner}</span><span class="banner-text"><span class="banner-title">${forced ? 'Сохраняем' : 'Пробный запрос'}</span><span class="banner-sub">${kind === 'cli' ? 'Проверяю, выполнен ли вход на сервере.' : forced ? 'Провайдер сохраняется без проверки.' : 'Запрашиваю у провайдера список моделей. Это занимает до 30 секунд.'}</span></span></div>`;
    let created;
    try { created = await api.createProvider(body); } catch (err) {
      inFlight = false;
      probe.innerHTML = '';
      if (forced) failBox.innerHTML = '';
      setBusyAll(false);
      fail(err);
      return;
    }
    inFlight = false;
    probe.innerHTML = '';
    setBusyAll(false);
    if (kind === 'cli') {
      if (created.status === 'ok') showSuccess(created, await modelsFound(created.id), false);
      else location.hash = `${HASH_LIST}/${created.id}/login`;
      return;
    }
    if (created.status === 'ok') {
      showSuccess(created, await modelsFound(created.id), created.kind === 'google_api');
      return;
    }
    showSavedWithoutProbe(created);
  }

  form.addEventListener('submit', (e) => {
    e.preventDefault();
    if (inFlight) return;
    reset();
    const body = {};
    if (kind === 'api') {
      const v = vendor();
      const key = $('#pa-key', form);
      const name = $('#pa-name', form);
      if (!key.value) { setFieldError(key, 'Введите ключ'); key.focus(); return; }
      if (!name.value.trim()) { setFieldError(name, 'Введите название'); name.focus(); return; }
      Object.assign(body, { kind: v.kind, name: name.value.trim(), secret: key.value });
    } else if (kind === 'endpoint') {
      const name = $('#pa-ep-name', form);
      const url = $('#pa-url', form);
      const key = $('#pa-ep-key', form);
      if (!name.value.trim()) { setFieldError(name, 'Введите название'); name.focus(); return; }
      if (!/^https?:\/\/[^\s/]+/i.test(url.value.trim())) { setFieldError(url, 'Адрес начинается с https:// и не содержит пробелов'); url.focus(); return; }
      if (!key.value) { setFieldError(key, 'Ключ нужен всегда: если сервер его не проверяет, введите любую строку'); key.focus(); return; }
      Object.assign(body, { kind: 'openai_compatible', name: name.value.trim(), base_url: url.value.trim(), secret: key.value });
    } else {
      const cli = $('#pa-cli-group [aria-checked="true"]', form).getAttribute('data-cli');
      const known = existing.find((p) => p.kind === 'cli_subscription' && p.cli === cli);
      if (known) { location.hash = `${HASH_LIST}/${known.id}/login`; return; }
      Object.assign(body, { kind: 'cli_subscription', cli, name: CLI_LABEL[cli] });
    }
    send(body, false);
  });
  setSubmitLabel();
}

// Радиогруппа со стрелками и общим tabindex: для списков выбора без своей обёртки.
function wireRadioGroup(group) {
  const radios = () => Array.from(group.querySelectorAll('[role="radio"]:not([disabled])'));
  const pick = (radio) => {
    group.querySelectorAll('[role="radio"]').forEach((r) => { r.setAttribute('aria-checked', String(r === radio)); r.tabIndex = r === radio ? 0 : -1; });
    radio.focus();
  };
  group.addEventListener('click', (e) => {
    const radio = e.target.closest('[role="radio"]');
    if (radio && !radio.disabled) pick(radio);
  });
  group.addEventListener('keydown', (e) => {
    const list = radios();
    const i = list.indexOf(document.activeElement);
    if (i < 0) return;
    const step = { ArrowDown: 1, ArrowRight: 1, ArrowUp: -1, ArrowLeft: -1 }[e.key];
    if (!step) return;
    e.preventDefault();
    pick(list[(i + step + list.length) % list.length]);
  });
}

// ---------------------------------------------------------------------------
// Выбор модели бота: лист на телефоне, блок на Mac
// ---------------------------------------------------------------------------
export async function loadModelOptions() {
  const [providers, models] = await Promise.all([api.listProviders(), api.listModels()]);
  return { providers, models, groups: modelGroups(providers, models) };
}

function groupNote(provider) {
  if (provider.kind === 'cli_subscription') return 'подписка, расход в квоту';
  if (provider.kind === 'openai_compatible') return 'свой адрес';
  return 'ключ, оплата по токенам';
}

// Провайдер с сотнями моделей: в выборе показываются первые PICK_LIMIT совпадений поиска, выбранная модель остаётся в списке всегда.
const PICK_LIMIT = 12;
const needsSearch = (groups) => groups.some((g) => g.models.length > PICK_LIMIT);

// Модели группы для показа: { items, matched } (matched: сколько нашлось всего, до обрезки).
function visibleModels(group, selectedId, query) {
  const big = group.models.length > PICK_LIMIT;
  const matched = group.models.filter(({ model }) => modelMatches(model, query));
  const items = big ? matched.slice(0, PICK_LIMIT) : matched;
  const pinned = selectedId && !items.some(({ model }) => model.id === selectedId) ? group.models.find(({ model }) => model.id === selectedId) : null;
  return { items: pinned ? [pinned, ...items] : items, matched: matched.length, big };
}

function optionsHtml(groups, selectedId, query = '') {
  let first = true;
  const views = groups.map((g) => ({ g, ...visibleModels(g, selectedId, query) })).filter((v) => v.items.length);
  const body = views.map(({ g, items, matched, big }) => `<div class="model-group">
      <div class="model-group-head"><span class="t-callout" data-i18n-skip>${esc(g.provider.name)}</span><span class="t-footnote">${esc(groupNote(g.provider))}</span></div>
      ${items.map(({ model, usable, reason }) => {
    const checked = model.id === selectedId;
    const tab = checked || (!selectedId && usable && first) ? 0 : -1;
    if (usable) first = false;
    return `<button type="button" role="radio" class="model-option" data-provider="${esc(g.provider.id)}" data-model="${esc(model.id)}" aria-checked="${checked}" tabindex="${tab}"${usable ? '' : ' disabled'}>
          <span class="radio-dot" aria-hidden="true"></span>
          <span class="model-option-text"><span class="model-option-title">${esc(modelTitle(model.name, model.display_name))}</span>${checked || reason ? `<span class="model-option-sub">${esc(checked ? `сейчас выбрана${reason ? ` · ${reason.toLowerCase()}` : ''}` : reason)}</span>` : ''}</span>
        </button>`;
  }).join('')}
      ${big && matched > PICK_LIMIT ? `<p class="t-footnote" data-pick-more>Показаны первые ${PICK_LIMIT} из ${matched}. Уточните поиск.</p>` : ''}
    </div>`).join('');
  const empty = views.length ? '' : '<p class="t-footnote" data-pick-empty>Ничего не найдено</p>';
  return `<div role="radiogroup" aria-label="Модель бота" class="stack gap-3">${body}${empty}</div>`;
}

// Поле поиска над выбором: только если у какого-то провайдера больше PICK_LIMIT моделей.
function pickSearchHtml(groups) {
  return needsSearch(groups) ? '<input type="search" class="input" data-pick-search placeholder="Поиск модели" aria-label="Поиск модели" autocomplete="off" autocapitalize="none" autocorrect="off" spellcheck="false">' : '';
}

function noModelsState() {
  return stateHtml({ iconHtml: ICONS.plug, title: 'Нет включённых моделей', text: 'Подключите провайдера и включите хотя бы одну модель.', actions: `<a class="btn btn-secondary" href="${HASH_LIST}">Открыть провайдеров</a>` });
}

function pickProblem(err) {
  if (err.status === 409) return { title: 'Сейчас идёт задача', text: 'Модель можно сменить, когда бот закончит работу.' };
  if (err.status === 400) return { title: 'Модель недоступна', text: 'Провайдер не отвечает или модель выключена. Обновите список в разделе «Провайдеры».' };
  if (err.status === 404) return { title: 'Бот не найден', text: 'Возможно, он уже удалён.' };
  return failure(err, 'Модель не изменена. ');
}

// Рисует выбор модели в container. opts: groups, selected() → id модели, onPick(provider, model) (бросает при отказе),
// current: подпись текущей модели вне реестра (старый бот), sheetSubtitle.
export function renderModelPicker(container, opts) {
  const { groups } = opts;
  const noteHtml = opts.note ? `<p class="t-footnote" data-pick-note>${esc(opts.note)}</p>` : '';
  if (!usableModels(groups).length) {
    container.innerHTML = noModelsState();
    return;
  }
  let selectedId = opts.selected();
  let query = '';
  const searchHtml = pickSearchHtml(groups);
  const find = (id) => groups.flatMap((g) => g.models.map((m) => ({ provider: g.provider, model: m.model }))).find((x) => x.model.id === id);
  const currentLabel = () => {
    const hit = find(selectedId);
    if (hit) return { title: modelTitle(hit.model.name, hit.model.display_name), sub: hit.provider.name };
    return { title: opts.current || 'Модель не выбрана', sub: opts.current ? 'не из ваших провайдеров' : 'Выберите из списка' };
  };

  async function pick(radio, alertBox, scope) {
    const hit = find(radio.getAttribute('data-model'));
    if (!hit || hit.model.id === selectedId) return;
    alertBox.innerHTML = '';
    scope.querySelectorAll('[role="radio"]').forEach((r) => { r.disabled = true; });
    try {
      await opts.onPick(hit.provider, hit.model);
      selectedId = hit.model.id;
    } catch (err) {
      const { title, text } = pickProblem(err);
      setAlert(alertBox, title, text);
    }
    const clicked = radio.getAttribute('data-model');
    scope.innerHTML = optionsHtml(groups, selectedId, query);
    wireOptions(scope, alertBox);
    paintTrigger();
    scope.querySelector(`[data-model="${clicked}"]`)?.focus();
  }
  function wireOptions(scope, alertBox) {
    const group = scope.querySelector('[role="radiogroup"]');
    wireRadioKeys(group);
    group.addEventListener('click', (e) => {
      const radio = e.target.closest('[role="radio"]');
      if (radio && !radio.disabled) pick(radio, alertBox, scope);
    });
  }
  // Поле поиска живёт вне списка: список перерисовывается при выборе, строка поиска остаётся.
  function wireSearch(scope, alertBox, input) {
    if (!input) return;
    input.addEventListener('input', () => {
      query = input.value;
      scope.innerHTML = optionsHtml(groups, selectedId, query);
      wireOptions(scope, alertBox);
    });
  }
  function paintTrigger() {
    const trigger = container.querySelector('[data-pick-open]');
    if (!trigger) return;
    const label = currentLabel();
    trigger.innerHTML = `<span class="stack gap-1 min-w-0"><span data-pick-title>${esc(label.title)}</span><span class="t-footnote">${esc(label.sub)}</span></span>${ICONS.chevronRight}`;
  }

  if (isDesktop()) {
    container.innerHTML = `${noteHtml}<div class="form-alert" data-pick-alert aria-live="polite"></div>${searchHtml}<div data-pick-body></div>`;
    const scope = container.querySelector('[data-pick-body]');
    scope.innerHTML = optionsHtml(groups, selectedId, query);
    wireOptions(scope, container.querySelector('[data-pick-alert]'));
    wireSearch(scope, container.querySelector('[data-pick-alert]'), container.querySelector('[data-pick-search]'));
    return;
  }
  container.innerHTML = '<button type="button" class="select-btn model-trigger" data-pick-open aria-haspopup="dialog"></button>';
  paintTrigger();
  container.querySelector('[data-pick-open]').addEventListener('click', () => {
    query = '';
    const dlg = openDialog({
      title: 'Модель бота',
      subtitle: opts.sheetSubtitle || 'Действует со следующего сообщения',
      content: `${noteHtml}<div class="form-alert" id="mp-alert" aria-live="polite"></div>${searchHtml}<div id="mp-body">${optionsHtml(groups, selectedId)}</div><div class="dialog-foot"><button type="button" class="btn btn-primary btn-block" data-close>Готово</button></div>`,
      focus: 'dialog',
    });
    wireOptions($('#mp-body', dlg.el), $('#mp-alert', dlg.el));
    wireSearch($('#mp-body', dlg.el), $('#mp-alert', dlg.el), dlg.el.querySelector('[data-pick-search]'));
    const start = dlg.el.querySelector('[role="radio"][aria-checked="true"]') || dlg.el.querySelector('[role="radio"]:not([disabled])');
    if (start) start.focus();
  });
}

// Стрелки внутри уже отрисованной радиогруппы: переходят между доступными вариантами и выбирают их.
function wireRadioKeys(group) {
  group.addEventListener('keydown', (e) => {
    const list = Array.from(group.querySelectorAll('[role="radio"]:not([disabled])'));
    const i = list.indexOf(document.activeElement);
    const step = { ArrowDown: 1, ArrowRight: 1, ArrowUp: -1, ArrowLeft: -1 }[e.key];
    if (i < 0 || !step) return;
    e.preventDefault();
    const next = list[(i + step + list.length) % list.length];
    next.focus();
    next.click();
  });
}

// Блок «Модель» в настройках бота: разметка и подключение.
export function botModelCardHtml(bot) {
  const legacy = !(bot.provider_id && bot.model_id);
  return `<div class="card card-pad stack gap-3" data-model-card>
    <span class="t-headline" style="font-size:15px;">Модель</span>
    <div data-model-picker aria-busy="true">${loadingHtml('Загружаю модели')}</div>
    <div data-model-problem></div>
    <span class="sr-only" data-model-label aria-hidden="true">${esc(bot.model ? modelTitle(bot.model) : 'модель не задана')}</span>
    ${botNoModel(bot) ? '<span class="t-footnote">Провайдер этого бота удалён. Выберите модель из своих провайдеров.</span>'
    : legacy && bot.model ? '<span class="t-footnote">Модель задана до подключения провайдеров. Выберите модель из своих провайдеров, чтобы бот не зависел от общего входа.</span>' : ''}
  </div>`;
}

export async function mountBotModelCard(scope, bot, onSaved) {
  const card = $('[data-model-card]', scope);
  if (!card) return;
  const holder = $('[data-model-picker]', card);
  const problemSlot = $('[data-model-problem]', card);
  let loaded = null;
  // Проблема провайдера текущей модели (ошибка проверки, ждёт администратора, сохранён без проверки): пока модель этого
  // провайдера, подсказка со ссылкой; новая модель её снимает.
  const paintProblem = () => {
    const provider = loaded && loaded.providers.find((x) => x.id === bot.provider_id);
    const waiting = provider && ['pending_admin', 'unchecked'].includes(provider.status);
    if (!provider || (provider.status !== 'error' && !waiting)) { problemSlot.innerHTML = ''; return; }
    const problem = waiting ? pendingProblem(provider) : providerProblem(provider);
    const tone = waiting || problem.code === 'login' ? 'banner-attention' : 'banner-danger';
    const sub = waiting
      ? 'Бот не ответит, пока провайдер не заработает. Дождитесь решения администратора или выберите модель другого провайдера.'
      : 'Бот не сможет отвечать на этой модели. Исправьте провайдера или выберите модель другого.';
    const title = waiting && problem.code === 'reapproval' ? `${provider.name}: ${problem.title.charAt(0).toLowerCase()}${problem.title.slice(1)}` : `${provider.name}: ${problem.short.toLowerCase()}`;
    problemSlot.innerHTML = `<div class="banner ${tone}" role="status" data-model-problem-banner><span class="banner-icon">${ICONS.alert}</span><span class="banner-text"><span class="banner-title">${esc(title)}</span><span class="banner-sub">${esc(sub)}</span></span><a class="btn btn-secondary" href="${HASH_LIST}/${esc(provider.id)}">Открыть провайдера</a></div>`;
  };
  const busy = bot.status === 'running';
  const draw = async () => {
    holder.setAttribute('aria-busy', 'true');
    holder.innerHTML = loadingHtml('Загружаю модели');
    let options;
    try { options = await loadModelOptions(); } catch (err) {
      if (err.status === 401) return;
      holder.removeAttribute('aria-busy');
      holder.innerHTML = stateHtml({ iconHtml: ICONS.cloudOff, title: 'Модели не загрузились', text: `Модель бота не изменена. ${failure(err).text}`, actions: retryButton, kind: 'state-error' });
      holder.querySelector('[data-act="retry"]').addEventListener('click', draw);
      return;
    }
    holder.removeAttribute('aria-busy');
    loaded = options;
    renderModelPicker(holder, {
      groups: options.groups,
      selected: () => bot.model_id,
      current: bot.provider_id ? '' : (bot.model ? modelTitle(bot.model) : ''),
      sheetSubtitle: busy ? `${bot.name} · идёт задача, модель можно сменить после неё` : `${bot.name} · действует со следующего сообщения`,
      note: busy ? 'Сейчас идёт задача. Модель можно сменить, когда бот закончит работу.' : '',
      onPick: async (provider, model) => {
        const saved = await api.patchBot(bot.id, { provider_id: provider.id, model_id: model.id });
        bot.provider_id = saved.provider_id ?? provider.id;
        bot.model_id = saved.model_id ?? model.id;
        onSaved(saved);
        paintProblem();
      },
    });
    paintProblem();
  };
  await draw();
}

// ---------------------------------------------------------------------------
// Состояния бота: нет модели, компьютер не запустился, перезапуск после задачи
// ---------------------------------------------------------------------------
export function botStateBanner(bot) {
  if (botNoModel(bot)) {
    return `<div class="banner banner-attention" role="status" data-bot-state="no_model"><span class="banner-icon">${ICONS.alert}</span><span class="banner-text"><span class="banner-title">Боту нужна модель</span><span class="banner-sub">Его провайдер удалён или отключён. Без модели бот не отвечает.</span></span><a class="btn btn-secondary" href="#/bots/${esc(bot.id)}">Выбрать модель</a></div>`;
  }
  if (bot.status === 'error_starting') {
    return `<div data-bot-state="error_starting" class="stack gap-2"><div class="banner banner-danger" role="alert"><span class="banner-icon">${ICONS.alert}</span><span class="banner-text"><span class="banner-title">Компьютер бота не запустился</span><span class="banner-sub">Пересоздание запускает компьютер бота заново. Файлы бота и сохранённые входы сохранятся. Пропадут только запущенные программы и всё, что было установлено вне папки с файлами бота.</span></span></div>
      <div class="form-alert" data-recreate-alert aria-live="polite"></div>
      <button type="button" class="btn btn-secondary" data-action="recreate-bot" data-bot="${esc(bot.id)}">${ICONS.retry}Пересоздать компьютер</button></div>`;
  }
  if (bot.status === 'starting') {
    return `<div class="banner banner-info" role="status" data-bot-state="starting" data-bot="${esc(bot.id)}"><span class="banner-icon spin">${ICONS.spinner}</span><span class="banner-text"><span class="banner-title">Бот запускается</span><span class="banner-sub">Компьютер бота создаётся. Сообщения можно писать: они подождут запуска.</span></span></div>`;
  }
  if (bot.need_restart) {
    return `<div class="banner banner-info" role="status" data-bot-state="need_restart"><span class="banner-icon">${ICONS.retry}</span><span class="banner-text"><span class="banner-title">Перезапустится после текущей задачи</span><span class="banner-sub">Вход в провайдера обновлён. Новые сообщения подождут перезапуска.</span></span></div>`;
  }
  return '';
}

// Подсказка под полем ввода, пока бот запускается: ввод открыт, сообщение встаёт в очередь (docs/contracts.md §9).
export function botStartHint(bot) {
  return bot.status === 'starting' ? '<p class="t-footnote composer-hint" role="status">Бот запускается: сообщение подождёт запуска.</p>' : '';
}

export async function recreateBotAction(button) {
  const holder = button.closest('[data-bot-state]');
  const box = holder && holder.querySelector('[data-recreate-alert]');
  const idle = button.innerHTML;
  button.disabled = true;
  button.setAttribute('aria-busy', 'true');
  button.innerHTML = `<span class="spin">${ICONS.spinner}</span>Пересоздаю`;
  if (box) box.innerHTML = '';
  try {
    await api.recreateBot(button.getAttribute('data-bot'));
  } catch (err) {
    button.disabled = false;
    button.removeAttribute('aria-busy');
    button.innerHTML = idle;
    const text = err.status === 502 || err.status === 503 ? 'Сервер компьютеров не ответил. Попробуйте позже.' : failure(err).text;
    if (box) setAlert(box, 'Компьютер не пересоздан', text);
    return;
  }
  context().rerender();
}

// Что связано с ботом: ядро удаляет только бота без тредов, памяти, расписаний и расхода (§10). Числа считаем заранее,
// чтобы объяснить причину до подтверждения. null: не удалось узнать, тогда решает ответ сервера.
async function botHistory(botId) {
  const [threads, schedules, memory] = await Promise.allSettled([api.listThreads(), api.listSchedules(), api.listMemory(botId)]);
  const own = (rows) => rows.filter((x) => x.bot_id === botId).length;
  return {
    threads: threads.status === 'fulfilled' ? own(threads.value) : null,
    schedules: schedules.status === 'fulfilled' ? own(schedules.value) : null,
    memory: memory.status === 'fulfilled' ? own(memory.value.active) + own(memory.value.proposed) : null,
  };
}

function historyReasons(h) {
  const reasons = [];
  if (h.threads) reasons.push(`${h.threads} ${plural(h.threads, 'тред', 'треда', 'тредов')}`);
  if (h.schedules) reasons.push(`${h.schedules} ${plural(h.schedules, 'расписание', 'расписания', 'расписаний')}`);
  if (h.memory) reasons.push(`${h.memory} ${plural(h.memory, 'запись памяти', 'записи памяти', 'записей памяти')}`);
  return reasons;
}

export async function confirmDeleteBot(bot) {
  let closed = false;
  const dlg = openDialog({ title: 'Удалить бота?', subtitle: bot.name, content: loadingHtml('Смотрю, что связано с ботом'), focus: 'dialog' });
  dlg.onClose = () => { closed = true; };
  const history = await botHistory(bot.id);
  const reasons = historyReasons(history);
  if (closed) return;
  if (reasons.length) {
    // Архивировать бота нельзя, а удалить сервер не даст: причины списком, подтверждения нет.
    dlg.setBody(`<p class="t-body confirm-text">Бот не удаляется, пока у него есть история. У этого бота есть:</p>
      <ul class="reason-list" aria-label="Что мешает удалению">${reasons.map((r) => `<li>${esc(r)}</li>`).join('')}</ul>
      <p class="t-footnote">Архивировать бота пока нельзя. Если он не нужен, остановите его: он перестанет работать, история останется.</p>
      <div class="btn-row"><button type="button" class="btn btn-secondary" data-close>Понятно</button></div>`);
    return;
  }
  const known = Object.values(history).every((v) => v !== null);
  dlg.setBody(confirmBody({
    text: `Компьютер бота удалится. Файлы бота и сохранённые входы сохранятся на сервере. Удалить можно только бота без истории: ${known ? 'у этого сейчас нет тредов, расписаний и памяти.' : 'если у него есть треды, расписания или память, сервер откажет.'}`,
    confirmLabel: 'Удалить бота',
  }));
  wireConfirm(dlg, {
    run: () => api.deleteBot(bot.id),
    describeError: (err) => {
      if (err.status === 409) return { title: 'У бота есть история', text: 'Бот не удалён: у него есть треды, память, расписания или расход. Остановите бота, если он не нужен.' };
      if (err.status === 404) return { title: 'Бот уже удалён', text: 'Вернитесь к списку ботов.' };
      if (err.status === 502) return { title: 'Компьютер не удалился', text: 'Сервер компьютеров не ответил. Бот не удалён, попробуйте позже.' };
      return failure(err, 'Бот не удалён. ');
    },
    done: () => { location.hash = '#/'; },
  });
}

export { runnerKind, kindLabel, modelCountText };
