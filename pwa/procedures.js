// procedures.js: раздел «Процедуры» (список, экран процедуры с правкой, запуск, выполнение), сохранение процедуры из turn.
// API: docs/contracts.md §14. Роутер зовёт viewProcedureRoute; тред зовёт openSaveProcedureDialog.
// Секреты: в интерфейсе только имена (secret_ref «vault:имя»), значений нет ни в разметке, ни в запросах.
// Риск шага вычисляет ядро (computed_risk): ниже вычисленного поле не даёт его выбрать, а ядро проверяет это ещё раз.
import * as api from './api.js';
import { ICONS, icon, esc, plural, fmtDateTime, alertHtml } from './ui.js';
import {
  context, field, setAlert, setBusy, clearErrors, setFieldError, failure, loadingHtml, stateHtml, retryButton,
  openDialog, confirmAction, segmentedHtml, wireSegmented, segmentedValue,
} from './account.js';
import { openThreadStream } from './ws.js';

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
const HASH_LIST = '#/procedures';
const procHash = (id) => `#/procedures/${encodeURIComponent(id)}`;
const runHash = (id) => `#/procedure-runs/${encodeURIComponent(id)}`;

const ACTIVE_RUN = ['queued', 'running', 'waiting_approval', 'waiting_model', 'waiting_human'];
const IMPORT_LIMIT = 1024 * 1024;

const SOURCE_LABEL = { bot: 'из действий бота', human: 'по показу человека', import: 'импорт' };
const SOURCE_FULL = { bot: 'Записана по действиям бота', human: 'Записана по показу человека', import: 'Импортирована из файла' };

const iconUp = icon('<path d="M12 19V5M5 12l7-7 7 7"></path>', 18, 2.2);
const iconDown = icon('<path d="M12 5v14M5 12l7 7 7-7"></path>', 18, 2.2);
const iconEdit = icon('<path d="M4 20h4L19 9l-4-4L4 16z"></path><path d="M13.5 6.5l4 4"></path>', 18, 2.2);
const iconCard = icon('<rect x="3" y="6" width="18" height="12" rx="2"></rect><path d="M3 10h18M7 15h3"></path>', 16, 2.2);
const iconSkip = icon('<path d="M5 12h14"></path>', 16, 2.4);
const iconDot = icon('<circle cx="12" cy="12" r="3"></circle>', 16, 2.4);
const iconCross = icon('<path d="M6 6l12 12M18 6L6 18"></path>', 16, 2.4);

// ---------------------------------------------------------------------------
// Риск: порядок «только вверх», подписи и значки. Равные по рангу уровни взаимозаменяемы.
// ---------------------------------------------------------------------------
const RISK = {
  none: { label: 'Без подтверждения' },
  other: { label: 'Другое действие', icon: ICONS.alert },
  send: { label: 'Отправка', icon: ICONS.send },
  push: { label: 'Публикация', icon: ICONS.share },
  exec: { label: 'Команда', icon: ICONS.terminal },
  delete: { label: 'Удаление', icon: ICONS.trash },
  login: { label: 'Вход', icon: ICONS.key },
  pay: { label: 'Оплата', icon: iconCard },
};
// Тот же порядок, что у ядра: выбрать можно уровень не ниже computed_risk, иначе ядро ответит 422 risk_below_computed.
const RISK_ORDER = ['none', 'other', 'send', 'push', 'exec', 'delete', 'login', 'pay'];
const riskRank = (risk) => { const i = RISK_ORDER.indexOf(risk || 'none'); return i < 0 ? 1 : i; };
const riskLabel = (risk) => (RISK[risk || 'none'] || { label: risk }).label;
const needsApproval = (step) => !!step.risk && step.risk !== 'none';
const riskFloor = (step) => step.computed_risk || step.risk || 'none';

// ---------------------------------------------------------------------------
// Шаги по-человечески
// ---------------------------------------------------------------------------
const ROLES = {
  button: { acc: 'кнопку', nom: 'кнопка', label: 'Кнопка' },
  link: { acc: 'ссылку', nom: 'ссылка', label: 'Ссылка' },
  textbox: { acc: 'поле', nom: 'поле', label: 'Поле ввода' },
  searchbox: { acc: 'поле поиска', nom: 'поле поиска', label: 'Поле поиска' },
  checkbox: { acc: 'флажок', nom: 'флажок', label: 'Флажок' },
  radio: { acc: 'переключатель', nom: 'переключатель', label: 'Переключатель' },
  switch: { acc: 'тумблер', nom: 'тумблер', label: 'Тумблер' },
  combobox: { acc: 'список', nom: 'список', label: 'Выпадающий список' },
  listbox: { acc: 'список', nom: 'список', label: 'Список' },
  option: { acc: 'вариант', nom: 'вариант', label: 'Вариант списка' },
  tab: { acc: 'вкладку', nom: 'вкладка', label: 'Вкладка' },
  menuitem: { acc: 'пункт меню', nom: 'пункт меню', label: 'Пункт меню' },
  heading: { acc: 'заголовок', nom: 'заголовок', label: 'Заголовок' },
  img: { acc: 'картинку', nom: 'картинка', label: 'Картинка' },
};
const CODE_ROLE = '__code';
const ACTIONS = [
  { value: 'navigate', label: 'Открыть адрес' },
  { value: 'click', label: 'Нажать' },
  { value: 'fill', label: 'Ввести в поле' },
  { value: 'press', label: 'Нажать клавишу' },
  { value: 'select', label: 'Выбрать в списке' },
  { value: 'wait', label: 'Подождать' },
  { value: 'assert', label: 'Проверить на странице' },
];
const ACTION_LABEL = Object.fromEntries(ACTIONS.map((a) => [a.value, a.label]));
const PARAM_TYPES = [{ value: 'string', label: 'Текст' }, { value: 'number', label: 'Число' }, { value: 'boolean', label: 'Да или нет' }];
const TYPE_LABEL = Object.fromEntries(PARAM_TYPES.map((t) => [t.value, t.label]));

const secretName = (ref) => String(ref || '').replace(/^vault:/, '');
const shortUrl = (url) => String(url || '').replace(/^https?:\/\//, '').replace(/\/$/, '');
const roleOf = (target) => ROLES[target && target.role];

// «кнопку «Войти»» / «элемент по коду»: падеж для действия (acc) или для проверки (nom).
function targetPhrase(target, kase = 'acc') {
  if (!target) return 'элемент';
  const role = roleOf(target);
  const word = role ? role[kase] : (target.role ? 'элемент' : 'элемент');
  if (target.name) return `${word} «${target.name}»`;
  if (target.selector && !target.role) return `${word} по коду`;
  return word;
}
const targetLabel = (target) => {
  if (!target) return '';
  const role = roleOf(target);
  const head = role ? role.label : (target.role ? 'Элемент' : 'Элемент');
  return target.name ? `${head} «${target.name}»` : head;
};

function stepTitle(step) {
  const t = step.target;
  switch (step.action) {
    case 'navigate': return `Открыть ${shortUrl(t && t.url) || 'страницу'}`;
    case 'click': return `Нажать ${targetPhrase(t)}`;
    case 'fill':
      if (step.secret_ref) return `Пароль из секрета «${secretName(step.secret_ref)}»`;
      if (step.value == null || step.value === '') return 'Ввести значение';
      return /^\{\{[^}]+\}\}$/.test(step.value) ? `Ввести ${step.value}` : `Ввести «${step.value}»`;
    case 'press': return `Нажать клавишу ${step.value || ''}`.trim();
    case 'select': return step.value ? `Выбрать «${step.value}»` : 'Выбрать вариант';
    case 'wait':
      if (step.value && t && (t.name || t.selector)) return `Подождать ${step.value} мс и дождаться: ${targetPhrase(t, 'nom')}`;
      if (t && (t.name || t.selector)) return `Дождаться: ${targetPhrase(t, 'nom')}`;
      return `Подождать ${step.value || ''} мс`.replace('  ', ' ');
    case 'assert': return `Проверить: ${targetPhrase(t, 'nom')} на странице`;
    default: return ACTION_LABEL[step.action] || String(step.action || 'Шаг');
  }
}

function conditionText(cond, expect) {
  const parts = [];
  if (!cond) return '';
  if (cond.url_matches) parts.push(`адрес подходит под ${cond.url_matches}`);
  if (cond.visible) parts.push(`виден ${targetPhrase(cond.visible, 'nom')}`);
  if (expect) {
    if (cond.text) parts.push(`есть текст «${cond.text}»`);
    if (cond.timeout_ms) parts.push(`ждать до ${cond.timeout_ms / 1000} с`.replace('.', ','));
  }
  return parts.join(', ');
}

// Подстрока под названием шага: куда нажимается или вводится, условия «было» и «получилось».
function stepSub(step) {
  const lines = [];
  const t = step.target;
  if ((step.action === 'fill' || step.action === 'select') && t && (t.name || t.role)) lines.push(`${targetLabel(t)}`);
  const was = conditionText(step.precondition, false);
  const got = conditionText(step.expect, true);
  if (was) lines.push(`Было: ${was}`);
  if (got) lines.push(`Получилось: ${got}`);
  return lines;
}

function riskBadge(step) {
  if (!needsApproval(step)) return '';
  const r = RISK[step.risk] || RISK.other;
  // flags: ядро хранит строгую метку в risk, остальные жёсткие метки шага («Вход» у шага с оплатой) лежат здесь.
  const extra = (Array.isArray(step.flags) ? step.flags : []).map((f) => (RISK[f] || RISK.other).label);
  return `<span class="badge badge-attention proc-badge" data-risk="${esc(step.risk)}">${r.icon || ICONS.alert}${esc(r.label)} · подтверждение${extra.length ? `<span class="sr-only"> и ещё: ${esc(extra.join(', '))}</span>` : ''}</span>${extra.length ? `<span class="badge badge-sunken proc-badge" data-flags="${esc(step.flags.join(' '))}">Ещё: ${esc(extra.join(', ').toLowerCase())}</span>` : ''}`;
}
const retryBadge = (step) => (step.safe_to_retry ? `<span class="badge badge-sunken proc-badge" data-retry>${ICONS.retry}Можно повторить</span>` : '');
const needsBadge = (step) => (step.needs_value ? `<span class="badge badge-danger proc-badge" data-needs-value>${ICONS.alert}Нужно заполнить</span>` : '');

const stepsToConfirm = (steps) => steps.map((s, i) => ({ step: s, n: i + 1 })).filter((x) => needsApproval(x.step));

// ---------------------------------------------------------------------------
// Ошибки ядра: {error, detail}. 422 приходит с путём поля в начале detail («steps[1].value: текст»).
// ---------------------------------------------------------------------------
const NOT_ENABLED = { title: 'Воспроизведение не включено', text: 'На сервере выключено воспроизведение процедур. Процедуры можно создавать и править, запуск станет доступен после включения.' };

// Коды причин ядра (docs/contracts.md §14, «Формат ошибок проверки»): detail «путь: код: короткий текст». Значений от
// клиента в нём нет, поэтому показываем только свои слова. Неизвестный код: показывается английский текст ядра как есть.
const ERROR_RU = {
  type: 'неверный тип значения',
  too_long: 'слишком длинное значение',
  too_many: 'слишком много элементов',
  empty: 'не может быть пустым',
  unknown_field: 'неизвестное поле',
  invalid: 'неверное значение',
  duplicate: 'такое значение уже есть',
  required: 'обязательное поле, заполните его',
  not_allowed: 'здесь это поле не допускается',
  mutually_exclusive: 'нельзя задать и значение, и секрет',
  placeholder_invalid: 'неверная подстановка: параметр пишется как {{имя}}',
  param_undeclared: 'параметр не описан в параметрах процедуры',
  param_in_selector: 'в селекторе параметры не работают: используйте подпись элемента',
  param_in_regex: 'в регулярном выражении подстановок нет',
  param_missing: 'у параметра нет значения',
  secret_param_misplaced: 'секретный параметр можно подставить только в значение поля ввода',
  secret_required: 'в это поле нужен секрет, а не значение: выберите «Секрет»',
  secret_not_found: 'секрет не найден',
  control_chars: 'в подписи есть управляющие или невидимые символы',
  url_forbidden: 'такой адрес открыть нельзя',
  risk_invalid: 'неизвестный уровень риска',
  risk_below_computed: 'риск ниже вычисленного сервером, понизить его нельзя',
  regex_invalid: 'не похоже на регулярное выражение',
  regex_too_long: 'регулярное выражение длиннее 300 символов',
  regex_nested: 'повтор внутри повторяющейся группы, такое выражение может зависнуть',
  regex_alternation: 'альтернатива («|») внутри повторяющейся группы, такое выражение может зависнуть',
  regex_backreference: 'обратные ссылки в выражении не допускаются',
  regex_lookaround: 'проверки «впереди» и «позади» в выражении не допускаются',
  regex_unbounded: 'в выражении больше одного неограниченного повтора (*, +, {n,})',
  regex_budget: 'повторы в выражении слишком большие',
  unsupported: 'формат файла не поддерживается',
  target_missing: 'у действия бота не записаны роль и подпись элемента',
  steps_need_values: 'в процедуре остались шаги, где нужно заполнить значение',
};
const CONFLICT_RU = {
  'name is already used': 'Процедура с таким названием уже есть.',
  'procedure has unfinished runs': 'У процедуры есть незавершённый запуск: дождитесь его конца или остановите.',
  'run is not waiting for a decision': 'Запуск уже не ждёт решения: экран обновится.',
};
const TOP_RU = { name: 'Название', description: 'Описание', steps: 'Шаги', params: 'Параметры', status: 'Статус', format: 'Формат файла', body: 'Файл', turn: 'Действия бота' };
const FIELD_RU = {
  id: 'идентификатор', action: 'действие', value: 'значение', secret_ref: 'секрет', risk: 'риск', safe_to_retry: 'повтор', target: 'цель',
  'target.url': 'адрес', 'target.role': 'роль элемента', 'target.name': 'подпись элемента', 'target.selector': 'селектор',
  'precondition.url_matches': 'условие «Было»: адрес', 'precondition.visible': 'условие «Было»: элемент', 'precondition.visible.name': 'условие «Было»: подпись',
  'expect.url_matches': 'условие «Получилось»: адрес', 'expect.visible': 'условие «Получилось»: элемент', 'expect.visible.name': 'условие «Получилось»: подпись',
  'expect.text': 'условие «Получилось»: текст', 'expect.timeout_ms': 'условие «Получилось»: время ожидания', needs_value: 'пометка «нужно заполнить»', needs_secret: 'пометка «нужен секрет»',
  name: 'имя', type: 'тип', required: 'обязательность', default: 'значение по умолчанию', secret: 'секрет',
};
// Место ошибки словами: «Шаг 3, подпись элемента». path приходит как steps.2.target.name.
function whereText(path) {
  if (!path) return '';
  const step = /^steps\.(\d+)(?:\.(.+))?$/.exec(path);
  if (step) return `Шаг ${Number(step[1]) + 1}${step[2] ? `, ${FIELD_RU[step[2]] || step[2]}` : ''}`;
  const param = /^params\.(\d+)(?:\.(.+))?$/.exec(path);
  if (param) return `Параметр ${Number(param[1]) + 1}${param[2] ? `, ${FIELD_RU[param[2]] || param[2]}` : ''}`;
  const event = /^events\.(\d+)(?:\.(.+))?$/.exec(path);
  if (event) return `Действие бота ${Number(event[1]) + 1}`;
  return TOP_RU[path] || path;
}

function parseDetail(detail) {
  const text = String(detail || '');
  const m = /^\s*([A-Za-z_]\w*(?:(?:\[\d+\]|\.\w+)+)?)\s*:\s*([\s\S]+)$/.exec(text);
  if (!m) return { path: '', code: '', where: '', message: text };
  const path = m[1].replace(/\[(\d+)\]/g, '.$1');
  let rest = m[2].trim();
  let code = '';
  const c = /^([a-z][a-z_]*):\s+([\s\S]*)$/.exec(rest);
  if (c) { code = c[1]; rest = c[2]; }
  return { path, code, where: whereText(path), message: ERROR_RU[code] || rest };
}

function procFailure(err, unsent = '') {
  if (err.status === 501 || err.code === 'not_implemented') return NOT_ENABLED;
  if (err.status === 404) return { title: 'Не найдено', text: 'Возможно, процедуру или запуск уже удалили.' };
  if (err.status === 422 || err.status === 400) {
    const { where, message } = parseDetail(err.detail);
    return { title: 'Сервер не принял значения', text: `${unsent}${where ? `${where}: ` : ''}${message}`.trim() };
  }
  if (err.status === 409) return { title: 'Конфликт', text: `${unsent}${CONFLICT_RU[err.detail] || err.detail || 'Состояние изменилось, обновите экран.'}`.trim() };
  return failure(err, unsent);
}

function loadFailure(err, what) {
  const { text } = failure(err);
  return stateHtml({ iconHtml: ICONS.cloudOff, title: `${what} не загрузились`, text, actions: retryButton, kind: 'state-error' });
}

// ---------------------------------------------------------------------------
// Формы: выбор, флажок, многострочное поле. Поле и подсказка связаны по id (setFieldError ищет «<id>-note»).
// ---------------------------------------------------------------------------
function selectField({ id, label, options, value, hint = '' }) {
  return `<div class="form-field">
    <label for="${id}">${esc(label)}</label>
    <div class="input-row"><select id="${id}" name="${id}" class="input" aria-describedby="${id}-note">${options.map((o) => `<option value="${esc(o.value)}"${String(o.value) === String(value) ? ' selected' : ''}${o.disabled ? ' disabled' : ''}>${esc(o.label)}</option>`).join('')}</select></div>
    <span id="${id}-note" class="field-note" data-hint="${esc(hint)}" aria-live="polite">${esc(hint)}</span>
  </div>`;
}
function checkField({ id, label, checked, hint = '' }) {
  return `<div class="form-field"><label class="check-row" for="${id}"><input type="checkbox" id="${id}" name="${id}" aria-describedby="${id}-note"${checked ? ' checked' : ''}><span>${esc(label)}</span></label>
    <span id="${id}-note" class="field-note" data-hint="${esc(hint)}" aria-live="polite">${esc(hint)}</span></div>`;
}
function textareaField({ id, label, value = '', hint = '', rows = 8 }) {
  return `<div class="form-field">
    <label for="${id}">${esc(label)}</label>
    <textarea id="${id}" name="${id}" class="input textarea mono" rows="${rows}" spellcheck="false" autocapitalize="none" autocorrect="off" aria-describedby="${id}-note">${esc(value)}</textarea>
    <span id="${id}-note" class="field-note" data-hint="${esc(hint)}" aria-live="polite">${esc(hint)}</span>
  </div>`;
}
const roleOptions = (selected, { code = true } = {}) => {
  const known = Object.entries(ROLES).map(([value, r]) => ({ value, label: r.label }));
  const extra = selected && !ROLES[selected] && selected !== CODE_ROLE ? [{ value: selected, label: `${selected} (по коду)` }] : [];
  return [...known, ...extra, ...(code ? [{ value: CODE_ROLE, label: 'Другая, по коду (в «Дополнительно»)' }] : [])];
};

// Имена секретов хранилища: список с сервера или null, если список недоступен (тогда имя вводится вручную).
function secretsLoader() {
  let promise = null;
  return () => {
    if (!promise) promise = api.listSecrets().catch(() => null);
    return promise;
  };
}

const lastRunText = (run) => {
  if (run === undefined) return '';
  if (!run) return 'Не запускалась';
  const when = fmtDateTime(run.finished_at || run.started_at || run.created_at);
  return `Запуск ${when}${RUN_STATUS[run.status] ? ` · ${RUN_STATUS[run.status].short}` : ''}`;
};

// ---------------------------------------------------------------------------
// Статусы запуска и шагов
// ---------------------------------------------------------------------------
const RUN_STATUS = {
  queued: { text: 'В очереди', short: 'В очереди', kind: 'neutral' },
  running: { text: 'Выполняется', short: 'Выполняется', kind: 'success' },
  waiting_approval: { text: 'Ждёт подтверждения', short: 'Ждёт подтверждения', kind: 'attention' },
  waiting_model: { text: 'Бот разбирается', short: 'Бот разбирается', kind: 'attention' },
  waiting_human: { text: 'Ваше решение', short: 'Ждёт вашего решения', kind: 'attention' },
  done: { text: 'Готово', short: 'Готово', kind: 'success' },
  failed: { text: 'Не удалось', short: 'Не удалось', kind: 'danger' },
  stopped: { text: 'Остановлен', short: 'Остановлен', kind: 'neutral' },
};
const STEP_STATE = {
  pending: 'Ожидает', running: 'Выполняется', waiting_approval: 'Ждёт подтверждения', waiting_model: 'Бот разбирается',
  waiting_human: 'Ваше решение', done: 'Готово', error: 'Ошибка', skipped: 'Пропущен', notrun: 'Не выполнялся',
};
const STEP_STATE_ICON = {
  done: `<span class="proc-state-icon is-done">${ICONS.check}</span>`,
  error: `<span class="proc-state-icon is-error">${iconCross}</span>`,
  running: `<span class="proc-state-icon spin">${ICONS.spinner}</span>`,
  waiting_approval: `<span class="proc-state-icon is-wait">${ICONS.alert}</span>`,
  waiting_model: `<span class="proc-state-icon is-wait">${ICONS.alert}</span>`,
  waiting_human: `<span class="proc-state-icon is-wait">${ICONS.alert}</span>`,
  skipped: `<span class="proc-state-icon is-muted">${iconSkip}</span>`,
  pending: `<span class="proc-state-icon is-muted">${iconDot}</span>`,
  notrun: `<span class="proc-state-icon is-muted">${iconDot}</span>`,
};
const isActiveRun = (run) => ACTIVE_RUN.includes(run.status);

function runErrorText(code) {
  const raw = String(code || '');
  if (raw === 'secret_not_found') return 'Секрет не найден. Проверьте имя секрета: он должен быть у бота запуска или общим.';
  if (raw === 'no_bot') return 'У запуска нет бота. Выберите бота при запуске.';
  if (raw === 'expect_failed') return 'После шага не выполнилось условие «Получилось».';
  if (raw === 'approval_rejected') return 'Подтверждение отклонено.';
  return raw;
}

const fmtDuration = (ms) => {
  if (ms == null) return '';
  if (ms < 1000) return `${Math.round(ms)} мс`;
  const sec = ms / 1000;
  if (sec < 60) return `${sec.toFixed(1).replace('.', ',')} с`;
  return `${Math.floor(sec / 60)} мин ${Math.round(sec % 60)} с`;
};
const runDuration = (run) => {
  const from = Date.parse(run.started_at || '');
  if (!from) return '';
  const to = Date.parse(run.finished_at || '') || Date.now();
  return fmtDuration(Math.max(0, to - from));
};

function stepStates(run, steps) {
  const log = new Map((run.step_log || []).map((e) => [e.step_id, e]));
  return steps.map((s, i) => {
    const entry = log.get(s.id) || null;
    if (entry) return { state: entry.status === 'ok' ? 'done' : entry.status === 'failed' ? 'error' : 'skipped', entry };
    if (i === run.next_step && isActiveRun(run)) return { state: run.status === 'queued' ? 'pending' : run.status, entry: null };
    if (i === run.next_step && run.status === 'failed') return { state: 'error', entry: null };
    return { state: isActiveRun(run) ? 'pending' : 'notrun', entry: null };
  });
}

// ---------------------------------------------------------------------------
// Вход из роутера
// ---------------------------------------------------------------------------
export async function viewProcedureRoute(route) {
  if (route.name === 'procedure-run') return viewRun(route.id);
  if (route.name === 'procedure') return viewDetail(route.id);
  return viewList();
}

// Делегирование кликов по [data-act] внутри каркаса экрана: он создаётся заново при каждой отрисовке.
function wireActs(handlers) {
  const scope = context().app.firstElementChild;
  scope.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-act]');
    if (!btn || btn.disabled || btn.getAttribute('aria-disabled') === 'true') return;
    const fn = handlers[btn.getAttribute('data-act')];
    if (fn) fn(btn, e);
  });
  return scope;
}

// ---------------------------------------------------------------------------
// Список процедур
// ---------------------------------------------------------------------------
function importButton(extra = '') {
  return `<button type="button" class="btn btn-secondary${extra}" data-act="import">${ICONS.share}Импорт из файла</button>`;
}

function procedureRow(p, botName) {
  const n = p.steps.length;
  const needs = p.steps.filter((s) => s.needs_value).length;
  const confirms = stepsToConfirm(p.steps).length;
  const status = p.status === 'draft' ? { text: `Черновик: ${n} ${plural(n, 'шаг', 'шага', 'шагов')}`, kind: 'attention' }
    : p.status === 'archived' ? { text: 'В архиве', kind: 'neutral' } : { text: 'Готова', kind: 'success' };
  const detail = p.status === 'draft'
    ? (needs ? `Нужно заполнить: ${needs}` : 'Проверьте шаги и отметьте готовой')
    : `${n} ${plural(n, 'шаг', 'шага', 'шагов')}${confirms ? ` · ${confirms} с подтверждением` : ''}`;
  const last = lastRunText(p.last_run);
  return `<li><a class="list-row proc-row" href="${procHash(p.id)}" data-proc-id="${esc(p.id)}">
    <span class="row-icon" aria-hidden="true">${ICONS.checklist}</span>
    <span class="row-body">
      <span class="row-title">${esc(p.name)}</span>
      <span class="row-sub">${esc(botName || 'Бот не выбран')} · ${esc(SOURCE_LABEL[p.source] || p.source)}</span>
      <span class="row-meta status-line"><span class="status-dot ${status.kind}"></span>${esc(status.text)}</span>
      <span class="row-meta">${esc(detail)}</span>
      ${last ? `<span class="row-meta" data-last-run>${esc(last)}</span>` : ''}
    </span>
    <span class="row-chevron" aria-hidden="true">${ICONS.chevronRight}</span>
  </a></li>`;
}

async function viewList() {
  const c = context();
  await c.frame({
    title: 'Процедуры',
    subtitle: 'Записанные действия ботов',
    backHref: '#/routines',
    activeNav: 'routines',
    body: `<div class="page-wide stack gap-4"><div class="desktop-only">${importButton()}</div><div class="stack gap-3" id="pl-content" aria-busy="true"></div></div>`,
    mobileActions: importButton(' btn-block'),
  });
  const content = $('#pl-content');
  wireActs({ import: () => openImportDialog() });
  content.addEventListener('click', (e) => { if (e.target.closest('[data-act="retry"]')) load(); });

  async function load() {
    content.setAttribute('aria-busy', 'true');
    content.innerHTML = loadingHtml('Загружаю процедуры');
    let procedures; let bots;
    try {
      [procedures, bots] = await Promise.all([api.listProcedures(), api.listBots().catch(() => [])]);
    } catch (err) {
      if (err.status === 401) return;
      content.removeAttribute('aria-busy');
      content.innerHTML = loadFailure(err, 'Процедуры');
      return;
    }
    content.removeAttribute('aria-busy');
    if (!procedures.length) {
      content.innerHTML = stateHtml({
        iconHtml: ICONS.checklist,
        title: 'Процедур пока нет',
        text: 'Процедура это записанные шаги в браузере бота: бот повторяет их без модели, а рискованные шаги ждут вашего подтверждения. Получить её можно из завершённого действия бота (кнопка «Сохранить как процедуру» под ответом в треде) или импортом из файла.',
        actions: `<button type="button" class="btn btn-secondary" data-act="import-empty">${ICONS.share}Импорт из файла</button>`,
      });
      $('[data-act="import-empty"]', content).addEventListener('click', () => openImportDialog());
      return;
    }
    const nameOf = (id) => (bots.find((b) => b.id === id) || {}).name;
    const live = procedures.filter((p) => p.status !== 'archived');
    const archived = procedures.filter((p) => p.status === 'archived');
    const section = (title, items) => (items.length ? `<h2 class="section-label">${title}</h2><ul class="list-plain">${items.map((p) => procedureRow(p, nameOf(p.bot_id))).join('')}</ul>` : '');
    content.innerHTML = `${section('Процедуры', live)}${section('В архиве', archived)}`;
  }
  await load();
}

// ---------------------------------------------------------------------------
// Импорт из файла (JSON): файл или вставленный текст; разбор до отправки, остальное проверяет ядро
// ---------------------------------------------------------------------------
function openImportDialog() {
  const idle = 'Импортировать';
  const dlg = openDialog({
    title: 'Импорт процедуры',
    subtitle: 'Файл JSON, сохранённый через «Экспорт»',
    content: `<form class="stack gap-4" id="im-form" novalidate>
      <div class="form-field"><label for="im-file">Файл</label><input type="file" id="im-file" class="input file-input" accept=".json,application/json" aria-describedby="im-file-note"><span id="im-file-note" class="field-note" data-hint="" aria-live="polite"></span></div>
      ${textareaField({ id: 'im-text', label: 'Или вставьте JSON', hint: 'Процедура появится черновиком: проверьте шаги и отметьте её готовой.' })}
      <div class="form-alert" id="im-alert" aria-live="polite"></div>
      <div class="btn-row btn-row-2"><button type="button" class="btn btn-secondary" data-close>Отмена</button><button type="submit" class="btn btn-primary" id="im-submit">${idle}</button></div>
    </form>`,
  });
  const form = $('#im-form', dlg.el);
  const text = $('#im-text', dlg.el);
  const fileNote = $('#im-file-note', dlg.el);
  $('#im-file', dlg.el).addEventListener('change', async (e) => {
    const file = e.target.files[0];
    fileNote.textContent = '';
    fileNote.classList.remove('is-error');
    if (!file) return;
    if (file.size > IMPORT_LIMIT) {
      fileNote.textContent = 'Файл больше 1 МБ: это не файл процедуры.';
      fileNote.classList.add('is-error');
      e.target.value = '';
      return;
    }
    try { text.value = await file.text(); } catch {
      fileNote.textContent = 'Файл не удалось прочитать.';
      fileNote.classList.add('is-error');
    }
  });
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    clearErrors(form);
    const raw = text.value.trim();
    if (!raw) { setFieldError(text, 'Выберите файл или вставьте JSON'); text.focus(); return; }
    if (raw.length > IMPORT_LIMIT) { setFieldError(text, 'Текст больше 1 МБ: это не процедура.'); text.focus(); return; }
    let doc;
    try { doc = JSON.parse(raw); } catch (err) {
      setFieldError(text, `Это не JSON: ${String(err.message || '').replace(/^JSON\.parse: /, '')}`);
      text.focus();
      return;
    }
    if (!doc || typeof doc !== 'object' || Array.isArray(doc)) { setFieldError(text, 'Ожидается один объект процедуры, а не список или значение.'); text.focus(); return; }
    const button = $('#im-submit', dlg.el);
    setBusy(button, true, 'Импортирую', idle);
    try {
      const created = await api.importProcedure(doc);
      dlg.close();
      location.hash = procHash(created.id);
    } catch (err) {
      setBusy(button, false, '', idle);
      const alertBox = $('#im-alert', dlg.el);
      if (err.status === 409) { setFieldError(text, 'Процедура с таким названием уже есть: переименуйте её в файле.'); text.focus(); return; }
      if (err.status === 422 || err.status === 400) {
        const { where, message } = parseDetail(err.detail);
        setFieldError(text, `${where ? `${where}: ` : ''}${message || 'файл не похож на процедуру'}`);
        text.focus();
        return;
      }
      const f = procFailure(err, 'Процедура не импортирована. ');
      setAlert(alertBox, f.title, f.text);
    }
  });
}

// ---------------------------------------------------------------------------
// Сохранение процедуры из turn (кнопка в треде)
// ---------------------------------------------------------------------------
export function openSaveProcedureDialog({ threadId, turnId }) {
  const dlg = openDialog({
    title: 'Сохранить как процедуру',
    subtitle: 'Действия бота в браузере станут шагами',
    content: `<form class="stack gap-4" id="sp-form" novalidate>
      ${field({ id: 'sp-name', label: 'Название процедуры', placeholder: 'Например, отклик на вакансию', hint: 'Процедура сохранится черновиком. Скрытые значения (пароли и другие поля ввода) нужно будет указать на её экране.' })}
      <div class="form-alert" id="sp-alert" aria-live="polite"></div>
      <div class="btn-row btn-row-2"><button type="button" class="btn btn-secondary" data-close>Отмена</button><button type="submit" class="btn btn-primary" id="sp-submit">Сохранить</button></div>
    </form>`,
  });
  const form = $('#sp-form', dlg.el);
  const input = $('#sp-name', dlg.el);
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    clearErrors(form);
    const name = input.value.trim();
    if (!name) { setFieldError(input, 'Введите название'); input.focus(); return; }
    const button = $('#sp-submit', dlg.el);
    setBusy(button, true, 'Сохраняю', 'Сохранить');
    try {
      const body = { thread_id: threadId, name };
      if (turnId) body.turn_id = turnId;
      const created = await api.createProcedureFromTurn(body);
      dlg.close();
      location.hash = procHash(created.id);
    } catch (err) {
      setBusy(button, false, '', 'Сохранить');
      if (err.status === 409) { setFieldError(input, 'Процедура с таким названием уже есть'); input.focus(); return; }
      if ((err.status === 422 || err.status === 400) && parseDetail(err.detail).path === 'name') {
        setFieldError(input, parseDetail(err.detail).message);
        input.focus();
        return;
      }
      const f = procFailure(err, 'Процедура не сохранена. ');
      setAlert($('#sp-alert', dlg.el), f.title, f.text);
    }
  });
}

// ---------------------------------------------------------------------------
// Экран процедуры
// ---------------------------------------------------------------------------
function runReason(p) {
  if (p.status === 'archived') return 'Процедура в архиве: верните её из архива, чтобы запускать.';
  if (p.status === 'draft') {
    const needs = p.steps.filter((s) => s.needs_value).length;
    return needs
      ? `Это черновик: заполните значения в шагах (${needs}) и отметьте процедуру готовой.`
      : 'Это черновик: проверьте шаги и отметьте процедуру готовой к запуску.';
  }
  if (!p.steps.length) return 'В процедуре нет шагов: добавьте хотя бы один.';
  return '';
}

function paramRow(prm, i) {
  const bits = [TYPE_LABEL[prm.type] || prm.type, prm.required ? 'обязательный' : 'необязательный'];
  if (prm.secret) bits.push('секрет: выбирается при запуске');
  else if (prm.default != null && prm.default !== '') bits.push(`по умолчанию: ${prm.default}`);
  return `<li class="proc-param" data-param="${esc(prm.name)}">
    <span class="stack flex-1 min-w-0"><span class="t-body proc-param-name">${esc(prm.name)}</span><span class="t-footnote">${esc(bits.join(' · '))}</span></span>
    <button type="button" class="icon-btn sunken" data-act="edit-param" data-index="${i}" aria-label="Изменить параметр ${esc(prm.name)}">${iconEdit}</button>
  </li>`;
}

function stepRow(step, i, total) {
  const sub = stepSub(step);
  const last = i === total - 1;
  const label = `Шаг ${i + 1}`;
  return `<li class="proc-step${step.needs_value ? ' needs-value' : ''}${needsApproval(step) ? ' has-risk' : ''}" data-step-id="${esc(step.id)}" data-index="${i}">
    <div class="proc-step-main">
      <span class="step-num" aria-hidden="true">${i + 1}</span>
      <div class="stack gap-1 flex-1 min-w-0">
        <span class="t-body proc-step-title"><span class="sr-only">${label}: </span>${esc(stepTitle(step))}</span>
        ${sub.map((l) => `<span class="t-footnote proc-step-sub">${esc(l)}</span>`).join('')}
        <span class="proc-badges">${riskBadge(step)}${retryBadge(step)}${needsBadge(step)}</span>
      </div>
    </div>
    <div class="proc-step-actions" role="group" aria-label="${label}: действия">
      <button type="button" class="icon-btn sunken" data-act="up" aria-label="${label}: выше"${i === 0 ? ' aria-disabled="true"' : ''}>${iconUp}</button>
      <button type="button" class="icon-btn sunken" data-act="down" aria-label="${label}: ниже"${last ? ' aria-disabled="true"' : ''}>${iconDown}</button>
      <button type="button" class="icon-btn sunken" data-act="edit-step" aria-label="${label}: изменить">${iconEdit}</button>
      <button type="button" class="icon-btn sunken" data-act="delete-step" aria-label="${label}: удалить">${ICONS.trash}</button>
    </div>
  </li>`;
}

function historyRow(run) {
  const st = RUN_STATUS[run.status] || { text: run.status, kind: 'neutral' };
  const dur = runDuration(run);
  return `<li><a class="list-row proc-run-row" href="${runHash(run.id)}" data-run-id="${esc(run.id)}">
    <span class="row-body">
      <span class="row-title">${esc(fmtDateTime(run.created_at))}</span>
      <span class="row-meta status-line"><span class="status-dot ${st.kind}"></span>${esc(st.text)}${dur ? ` · ${esc(dur)}` : ''}</span>
      ${run.status === 'failed' && run.error ? `<span class="row-meta wrap">${esc(runErrorText(run.error))}</span>` : ''}
    </span>
    <span class="row-chevron" aria-hidden="true">${ICONS.chevronRight}</span>
  </a></li>`;
}

async function viewDetail(id) {
  const c = context();
  let proc; let bots; let runs;
  try {
    [proc, bots, runs] = await Promise.all([api.getProcedure(id), api.listBots().catch(() => []), api.listProcedureRuns(id).catch(() => null)]);
  } catch (err) {
    if (err.status === 401) return;
    const missing = err.status === 404;
    await c.frame({
      title: 'Процедура', subtitle: 'Процедуры', backHref: HASH_LIST, activeNav: 'routines',
      body: `<div class="page-narrow">${missing
        ? stateHtml({ iconHtml: ICONS.checklist, title: 'Процедура не найдена', text: 'Возможно, её уже удалили.', actions: `<a class="btn btn-secondary" href="${HASH_LIST}">К процедурам</a>` })
        : loadFailure(err, 'Процедура')}</div>`,
    });
    $('[data-act="retry"]')?.addEventListener('click', () => c.rerender());
    return;
  }
  const secrets = secretsLoader();
  const botName = (bid) => (bots.find((b) => b.id === bid) || {}).name;

  await c.frame({
    title: proc.name,
    subtitle: `Процедура · ${botName(proc.bot_id) || 'бот не выбран'} · версия ${proc.version}`,
    backHref: HASH_LIST,
    activeNav: 'routines',
    body: '<div class="page-narrow stack gap-4" id="pr-root"></div>',
    mobileActions: '<div class="proc-bar" data-bar></div>',
  });
  const root = $('#pr-root');
  const bar = $('[data-bar]');
  let pending = 0;
  let queue = Promise.resolve();

  const statusBadge = () => (proc.status === 'draft' ? '<span class="badge badge-attention">Черновик</span>'
    : proc.status === 'archived' ? '<span class="badge badge-sunken">В архиве</span>' : '<span class="badge badge-success">Готова</span>');

  function barHtml() {
    const reason = runReason(proc);
    return `<button type="button" class="btn btn-primary btn-block" data-act="run"${reason ? ' disabled aria-describedby="pr-reason"' : ''}>${ICONS.play}Запустить</button>`;
  }

  function render(focusSel) {
    const reason = runReason(proc);
    const needs = proc.steps.filter((s) => s.needs_value).length;
    const confirms = stepsToConfirm(proc.steps).length;
    const kv = (k, v) => `<div class="proc-kv"><dt>${esc(k)}</dt><dd>${esc(v)}</dd></div>`;
    root.innerHTML = `
      <div class="form-alert" id="pr-alert" aria-live="polite"></div>
      <section class="card card-pad stack gap-3" aria-labelledby="pr-title">
        <div class="title-line"><h2 class="t-headline" id="pr-title" style="overflow-wrap:anywhere;">${esc(proc.name)}</h2>${statusBadge()}</div>
        ${proc.description ? `<p class="t-body" style="margin:0;">${esc(proc.description)}</p>` : ''}
        <dl class="proc-kvs">
          ${kv('Бот', botName(proc.bot_id) || 'не выбран')}
          ${kv('Версия', String(proc.version))}
          ${kv('Источник', SOURCE_FULL[proc.source] || proc.source)}
          ${kv('Шагов', `${proc.steps.length}${confirms ? `, с подтверждением ${confirms}` : ''}`)}
        </dl>
        <div class="row gap-2 wrap-row">
          <button type="button" class="btn btn-secondary" data-act="edit-meta">${iconEdit}Название и бот</button>
        </div>
        <div class="desktop-only stack gap-2" data-desktop-run>${barHtml()}</div>
        ${reason ? `<p class="t-footnote" id="pr-reason" data-run-reason>${esc(reason)}</p>` : ''}
        ${proc.status === 'draft' ? `<button type="button" class="btn btn-secondary btn-block" data-act="ready"${needs ? ' disabled aria-describedby="pr-reason"' : ''}>${ICONS.check}Готова к запуску</button>` : ''}
      </section>
      <section class="card card-pad stack gap-3" aria-labelledby="pr-params-h">
        <div class="row gap-2"><h2 class="t-headline flex-1" id="pr-params-h">Параметры</h2><button type="button" class="btn btn-secondary" data-act="add-param">${ICONS.plus}Параметр</button></div>
        ${proc.params.length
    ? `<ul class="list-plain proc-params">${proc.params.map(paramRow).join('')}</ul>`
    : '<p class="t-footnote" style="margin:0;">Параметров нет: значения в шагах постоянные. Параметр позволяет менять значение при каждом запуске, например адрес почты.</p>'}
      </section>
      <section class="card card-pad stack gap-3" aria-labelledby="pr-steps-h">
        <div class="row gap-2"><h2 class="t-headline flex-1" id="pr-steps-h">Шаги</h2><button type="button" class="btn btn-secondary" data-act="add-step">${ICONS.plus}Шаг</button></div>
        <p class="t-footnote" style="margin:0;">Порядок меняют кнопки «выше» и «ниже» или Alt и стрелка вверх или вниз на выбранном шаге.</p>
        ${needs ? `<div class="banner banner-attention" role="status"><span class="banner-icon">${ICONS.alert}</span><span class="banner-text"><span class="banner-title">Нужно заполнить: ${needs} ${plural(needs, 'шаг', 'шага', 'шагов')}</span><span class="banner-sub">Значения, которые бот вводил в поля, не сохраняются. Откройте шаг и укажите значение, параметр или секрет.</span></span></div>` : ''}
        ${proc.steps.length
    ? `<ol class="proc-steps" id="pr-steps">${proc.steps.map((s, i) => stepRow(s, i, proc.steps.length)).join('')}</ol>`
    : '<p class="t-footnote" style="margin:0;">Шагов нет. Добавьте первый шаг.</p>'}
      </section>
      <section class="stack gap-3" aria-labelledby="pr-runs-h">
        <h2 class="section-label" id="pr-runs-h">История запусков</h2>
        <div id="pr-runs" class="stack gap-2">${runsHtml()}</div>
      </section>
      <section class="card card-pad stack gap-2" aria-labelledby="pr-more-h">
        <h2 class="t-headline" id="pr-more-h">Действия</h2>
        <div class="proc-more">
          <button type="button" class="btn btn-secondary" data-act="export">${ICONS.download}Экспорт в JSON</button>
          <button type="button" class="btn btn-secondary" data-act="archive">${proc.status === 'archived' ? 'Вернуть из архива' : 'В архив'}</button>
          <button type="button" class="btn btn-danger" data-act="delete">${ICONS.trash}Удалить</button>
        </div>
      </section>`;
    if (bar) bar.innerHTML = barHtml();
    if (focusSel) { const el = $(focusSel, root); if (el) el.focus(); }
  }

  function runsHtml() {
    if (runs === null) return '<p class="t-footnote" style="margin:0;">История запусков не загрузилась.</p>';
    if (!runs.length) return '<p class="t-footnote" style="margin:0;">Запусков ещё не было.</p>';
    return `<ul class="list-plain">${runs.map(historyRow).join('')}</ul>`;
  }

  // Живая область вне перерисовываемого корня: иначе читалка экрана не слышит текст в только что созданном узле.
  const live = document.createElement('div');
  live.className = 'sr-only';
  live.id = 'pr-live';
  live.setAttribute('role', 'status');
  live.setAttribute('aria-live', 'polite');
  root.after(live);
  const alertBox = () => $('#pr-alert', root);
  const announce = (text) => { live.textContent = text; };

  // Правка идёт по очереди: быстрые перестановки подряд не обгоняют друг друга, ответ принимается после последней.
  function save(patch, { local = false } = {}) {
    pending += 1;
    const p = queue.then(() => api.patchProcedure(proc.id, patch));
    queue = p.catch(() => {});
    return p.then((saved) => {
      pending -= 1;
      if (!local || pending === 0) { proc = saved; }
      return saved;
    }, (err) => { pending -= 1; throw err; });
  }

  async function reloadFromServer() {
    try { proc = await api.getProcedure(proc.id); } catch { /* остаётся локальное */ }
    render();
  }

  function moveStep(index, dir, act) {
    const to = index + dir;
    if (to < 0 || to >= proc.steps.length) return;
    const steps = proc.steps.slice();
    [steps[index], steps[to]] = [steps[to], steps[index]];
    const moved = steps[to];
    proc = { ...proc, steps };
    alertBox().innerHTML = '';
    render(`[data-step-id="${CSS.escape(moved.id)}"] [data-act="${act}"]`);
    announce(`Шаг «${stepTitle(moved)}» теперь ${to + 1} из ${steps.length}`);
    save({ steps: steps.map(cleanStep) }, { local: true }).then(() => {
      if (pending === 0) { const focused = document.activeElement && document.activeElement.getAttribute('data-act'); render(focused ? `[data-step-id="${CSS.escape(moved.id)}"] [data-act="${focused}"]` : null); }
    }).catch(async (err) => {
      const f = procFailure(err, 'Порядок не сохранён. ');
      await reloadFromServer();
      setAlert(alertBox(), f.title, f.text);
    });
  }

  root.addEventListener('keydown', (e) => {
    if (!e.altKey || (e.key !== 'ArrowUp' && e.key !== 'ArrowDown')) return;
    const li = e.target.closest('.proc-step');
    if (!li) return;
    e.preventDefault();
    const act = e.key === 'ArrowUp' ? 'up' : 'down';
    moveStep(Number(li.getAttribute('data-index')), e.key === 'ArrowUp' ? -1 : 1, act);
  });

  const onAct = async (btn) => {
    const act = btn.getAttribute('data-act');
    const li = btn.closest('.proc-step');
    const index = li ? Number(li.getAttribute('data-index')) : -1;
    if (act === 'up') moveStep(index, -1, 'up');
    else if (act === 'down') moveStep(index, 1, 'down');
    else if (act === 'edit-step') openStepDialog({ proc, index, secrets, onSaved: (saved) => { proc = saved; render(`[data-step-id="${CSS.escape(saved.steps[index].id)}"] [data-act="edit-step"]`); } });
    else if (act === 'add-step') openStepDialog({ proc, index: -1, secrets, onSaved: (saved) => { proc = saved; render('#pr-steps li:last-child [data-act="edit-step"]'); } });
    else if (act === 'delete-step') confirmDeleteStep(index);
    else if (act === 'edit-param') openParamDialog({ proc, index: Number(btn.getAttribute('data-index')), onSaved: (saved) => { proc = saved; render(); } });
    else if (act === 'add-param') openParamDialog({ proc, index: -1, onSaved: (saved) => { proc = saved; render(); } });
    else if (act === 'edit-meta') openMetaDialog({ proc, bots, onSaved: () => c.rerender() });
    else if (act === 'ready') await setStatus(btn, 'active');
    else if (act === 'archive') await setStatus(btn, proc.status === 'archived' ? 'active' : 'archived');
    else if (act === 'export') await doExport(btn);
    else if (act === 'delete') confirmDeleteProcedure();
    else if (act === 'run') openRunSheet({ proc, bots, secrets, onStarted: () => {} });
  };
  root.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-act]');
    if (!btn || btn.disabled || btn.getAttribute('aria-disabled') === 'true') return;
    onAct(btn);
  });
  if (bar) {
    bar.addEventListener('click', (e) => {
      const btn = e.target.closest('[data-act]');
      if (btn && !btn.disabled) onAct(btn);
    });
  }

  async function setStatus(btn, status) {
    alertBox().innerHTML = '';
    const idleHtml = btn.innerHTML;
    setBusy(btn, true, 'Сохраняю', idleHtml);
    try {
      proc = await save({ status });
      render(`[data-act="${btn.getAttribute('data-act')}"]`);
    } catch (err) {
      setBusy(btn, false, '', idleHtml);
      const f = procFailure(err, 'Статус не изменён. ');
      setAlert(alertBox(), f.title, f.text);
    }
  }

  async function doExport(btn) {
    alertBox().innerHTML = '';
    const idleHtml = btn.innerHTML;
    setBusy(btn, true, 'Готовлю файл', idleHtml);
    try {
      const doc = await api.exportProcedure(proc.id);
      downloadJson(proc.name, doc);
    } catch (err) {
      const f = procFailure(err, 'Файл не получен. ');
      setAlert(alertBox(), f.title, f.text);
    }
    setBusy(btn, false, '', idleHtml);
  }

  function confirmDeleteStep(index) {
    const step = proc.steps[index];
    confirmAction({
      title: 'Удалить шаг?',
      subtitle: `Шаг ${index + 1}`,
      text: `Шаг «${stepTitle(step)}» исчезнет из процедуры. Прежние запуски сохранят свою версию.`,
      confirmLabel: 'Удалить шаг',
      run: () => save({ steps: proc.steps.filter((_, i) => i !== index).map(cleanStep) }),
      describeError: (err) => procFailure(err, 'Шаг не удалён. '),
      done: () => render('[data-act="add-step"]'),
    });
  }

  function confirmDeleteProcedure() {
    confirmAction({
      title: 'Удалить процедуру?',
      subtitle: proc.name,
      text: 'Процедура и история её запусков будут удалены без возможности восстановления. Сохранить копию можно через «Экспорт».',
      confirmLabel: 'Удалить процедуру',
      run: () => api.deleteProcedure(proc.id),
      describeError: (err) => (err.status === 409
        ? { title: 'Нельзя удалить: идёт запуск', text: 'Остановите активный запуск на его экране в истории запусков и повторите.' }
        : procFailure(err, 'Процедура не удалена. ')),
      done: () => { location.hash = HASH_LIST; },
    });
  }

  render();
}

// Шаг без вычисленных ядром полей (computed_risk, flags): их в запрос не отправляем.
function cleanStep(step) {
  const { computed_risk: _computed, flags: _flags, ...rest } = step;
  return rest;
}

function downloadJson(name, doc) {
  const blob = new Blob([JSON.stringify(doc, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `${String(name).replace(/[\\/:*?"<>|\s]+/g, '-').replace(/^-+|-+$/g, '') || 'procedure'}.procedure.json`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// ---------------------------------------------------------------------------
// Название, описание, бот
// ---------------------------------------------------------------------------
function openMetaDialog({ proc, bots, onSaved }) {
  const botOptions = [{ value: '', label: 'Без бота (выбирается при запуске)' }, ...bots.map((b) => ({ value: b.id, label: b.name }))];
  const dlg = openDialog({
    title: 'Название и бот',
    content: `<form class="stack gap-4" id="mt-form" novalidate>
      ${field({ id: 'mt-name', label: 'Название', value: proc.name })}
      ${field({ id: 'mt-desc', label: 'Описание', value: proc.description || '', required: false })}
      ${selectField({ id: 'mt-bot', label: 'Бот по умолчанию', options: botOptions, value: proc.bot_id || '' })}
      <div class="form-alert" id="mt-alert" aria-live="polite"></div>
      <div class="btn-row btn-row-2"><button type="button" class="btn btn-secondary" data-close>Отмена</button><button type="submit" class="btn btn-primary" id="mt-submit">Сохранить</button></div>
    </form>`,
  });
  const form = $('#mt-form', dlg.el);
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    clearErrors(form);
    const name = $('#mt-name', dlg.el);
    if (!name.value.trim()) { setFieldError(name, 'Введите название'); name.focus(); return; }
    const patch = { name: name.value.trim(), description: $('#mt-desc', dlg.el).value.trim(), bot_id: $('#mt-bot', dlg.el).value || null };
    const button = $('#mt-submit', dlg.el);
    setBusy(button, true, 'Сохраняю', 'Сохранить');
    try {
      await api.patchProcedure(proc.id, patch);
    } catch (err) {
      setBusy(button, false, '', 'Сохранить');
      if (err.status === 409) { setFieldError(name, 'Процедура с таким названием уже есть'); name.focus(); return; }
      const { path, message } = parseDetail(err.detail);
      if ((err.status === 422 || err.status === 400) && path === 'name') { setFieldError(name, message); name.focus(); return; }
      const f = procFailure(err, 'Изменения не сохранены. ');
      setAlert($('#mt-alert', dlg.el), f.title, f.text);
      return;
    }
    dlg.close();
    onSaved();
  });
}

// ---------------------------------------------------------------------------
// Параметры процедуры
// ---------------------------------------------------------------------------
function openParamDialog({ proc, index, onSaved }) {
  const isNew = index < 0;
  const prm = isNew ? { name: '', type: 'string', required: false, default: null, secret: false } : proc.params[index];
  const dlg = openDialog({
    title: isNew ? 'Новый параметр' : 'Параметр',
    subtitle: isNew ? '' : prm.name,
    content: `<form class="stack gap-4" id="pm-form" novalidate>
      ${field({ id: 'pm-name', label: 'Имя', value: prm.name, mono: true, hint: 'В шагах используется как {{имя}}: латинские буквы, цифры, «_» и «-».' })}
      ${selectField({ id: 'pm-type', label: 'Тип', options: PARAM_TYPES, value: prm.type })}
      ${checkField({ id: 'pm-required', label: 'Обязательный: без значения запуск не начнётся', checked: prm.required })}
      ${checkField({ id: 'pm-secret', label: 'Секрет: при запуске выбирается из секретов, значение не вводится', checked: prm.secret })}
      <div id="pm-default-wrap">${field({ id: 'pm-default', label: 'Значение по умолчанию', value: prm.default == null ? '' : String(prm.default), required: false })}</div>
      <div class="form-alert" id="pm-alert" aria-live="polite"></div>
      <div class="btn-row ${isNew ? 'btn-row-2' : 'btn-row-3'}">
        <button type="button" class="btn btn-secondary" data-close>Отмена</button>
        ${isNew ? '' : '<button type="button" class="btn btn-danger" id="pm-delete">Удалить</button>'}
        <button type="submit" class="btn btn-primary" id="pm-submit">Сохранить</button>
      </div>
    </form>`,
  });
  const form = $('#pm-form', dlg.el);
  const secret = $('#pm-secret', dlg.el);
  const wrap = $('#pm-default-wrap', dlg.el);
  const syncSecret = () => { wrap.hidden = secret.checked; };
  secret.addEventListener('change', syncSecret);
  syncSecret();

  async function persist(params, button, idle) {
    setBusy(button, true, 'Сохраняю', idle);
    try {
      const saved = await api.patchProcedure(proc.id, { params });
      dlg.close();
      onSaved(saved);
    } catch (err) {
      setBusy(button, false, '', idle);
      const { path, message } = parseDetail(err.detail);
      const target = { 'params.name': 'pm-name', 'params.default': 'pm-default' }[path.replace(/^params\.\d+/, 'params')];
      if ((err.status === 422 || err.status === 400) && target && $(`#${target}`, dlg.el)) {
        setFieldError($(`#${target}`, dlg.el), message);
        $(`#${target}`, dlg.el).focus();
        return;
      }
      const f = procFailure(err, 'Параметры не сохранены. ');
      setAlert($('#pm-alert', dlg.el), f.title, f.text);
    }
  }

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    clearErrors(form);
    const name = $('#pm-name', dlg.el);
    const value = name.value.trim();
    if (!value) { setFieldError(name, 'Введите имя параметра'); name.focus(); return; }
    if (/[\s{}]/.test(value)) { setFieldError(name, 'В имени нельзя использовать пробелы и фигурные скобки'); name.focus(); return; }
    if (proc.params.some((x, i) => i !== index && x.name === value)) { setFieldError(name, 'Параметр с таким именем уже есть'); name.focus(); return; }
    const type = $('#pm-type', dlg.el).value;
    const isSecret = secret.checked;
    const rawDefault = $('#pm-default', dlg.el).value;
    const next = { name: value, type, required: $('#pm-required', dlg.el).checked, default: null, secret: isSecret };
    if (!isSecret && rawDefault.trim() !== '') {
      if (type === 'number') {
        if (!Number.isFinite(Number(rawDefault))) { setFieldError($('#pm-default', dlg.el), 'Нужно число'); $('#pm-default', dlg.el).focus(); return; }
        next.default = Number(rawDefault);
      } else if (type === 'boolean') {
        const v = rawDefault.trim().toLowerCase();
        if (!['true', 'false', 'да', 'нет'].includes(v)) { setFieldError($('#pm-default', dlg.el), 'Нужно «да» или «нет»'); $('#pm-default', dlg.el).focus(); return; }
        next.default = v === 'true' || v === 'да';
      } else next.default = rawDefault;
    }
    const params = proc.params.slice();
    if (isNew) params.push(next); else params[index] = next;
    await persist(params, $('#pm-submit', dlg.el), 'Сохранить');
  });
  $('#pm-delete', dlg.el)?.addEventListener('click', () => {
    const using = proc.steps.filter((s) => String(s.value || '').includes(`{{${prm.name}}}`)).length;
    const params = proc.params.filter((_, i) => i !== index);
    if (using) {
      setAlert($('#pm-alert', dlg.el), 'Параметр используется', `Он указан в шагах (${using}). Сначала замените его в этих шагах.`);
      return;
    }
    persist(params, $('#pm-delete', dlg.el), 'Удалить');
  });
}

// ---------------------------------------------------------------------------
// Правка шага
// ---------------------------------------------------------------------------
const nextStepId = (steps) => `s${steps.reduce((max, s) => Math.max(max, Number((String(s.id).match(/^s(\d+)$/) || [])[1]) || 0), steps.length) + 1}`;

const PARAM_REF = /^\{\{\s*([^}\s]+)\s*\}\}$/;

function stepFormHtml(step, proc, secretNames) {
  const t = step.target || {};
  // Черновик из действий бота с пометкой needs_secret (пароль, данные карты, код, безымянное поле): сразу «Секрет».
  const valueSource = step.secret_ref ? 'secret' : (PARAM_REF.test(step.value || '') && proc.params.some((p) => p.name === PARAM_REF.exec(step.value)[1])) ? 'param'
    : (step.needs_secret && (step.value == null || step.value === '')) ? 'secret' : 'text';
  const knownRole = ROLES[t.role] ? t.role : (t.role ? t.role : 'button');
  const rawRole = t.role && !ROLES[t.role];
  const floor = riskFloor(step);
  const riskHint = floor === 'none'
    ? 'Выше ставить можно, сервер проверит уровень при сохранении.'
    : `Сервер вычислил «${riskLabel(floor)}». Ниже этого уровня риск понизить нельзя.`;
  const vis = (cond) => (cond && cond.visible) || {};
  const pre = step.precondition || {};
  const exp = step.expect || {};
  const paramOptions = proc.params.length
    ? proc.params.map((p) => ({ value: p.name, label: `${p.name}${p.secret ? ' (секрет)' : ''}` }))
    : [{ value: '', label: 'Параметров пока нет' }];
  const secretField = secretNames
    ? selectField({ id: 'sf-secret', label: 'Секрет из хранилища', value: secretName(step.secret_ref),
      options: [{ value: '', label: 'Выберите секрет' }, ...secretNames.map((n) => ({ value: n, label: n })),
        ...(step.secret_ref && !secretNames.includes(secretName(step.secret_ref)) ? [{ value: secretName(step.secret_ref), label: `${secretName(step.secret_ref)} (не найден)` }] : [])],
      hint: 'Пароль не вводится здесь: шаг берёт его из хранилища в момент запуска.' })
    : field({ id: 'sf-secret', label: 'Имя секрета', value: secretName(step.secret_ref), mono: true, required: false, hint: 'Список секретов недоступен: введите имя секрета. Сам пароль вводить не нужно.' });
  const sourceOptions = [{ value: 'text', label: 'Значение' }, { value: 'param', label: 'Параметр' }, { value: 'secret', label: 'Секрет' }];
  return `<form class="stack gap-4" id="sf-form" novalidate>
    ${selectField({ id: 'sf-action', label: 'Действие', options: ACTIONS, value: step.action || 'click' })}
    <div class="stack gap-4" data-for="navigate">${field({ id: 'sf-url', label: 'Адрес страницы', value: t.url || '', mono: true, inputmode: 'url', hint: 'Можно использовать параметры: {{имя}}.' })}</div>
    <div class="stack gap-4" data-for="click fill select wait assert">
      ${selectField({ id: 'sf-role', label: 'Что найти на странице', options: roleOptions(rawRole ? CODE_ROLE : knownRole), value: rawRole ? CODE_ROLE : knownRole })}
      ${field({ id: 'sf-name', label: 'Подпись элемента', value: t.name || '', required: false, hint: 'Так элемент подписан на странице, например «Войти».' })}
    </div>
    <div class="stack gap-4" data-for="press">${field({ id: 'sf-key', label: 'Клавиша', value: step.action === 'press' ? (step.value || '') : '', mono: true, hint: 'Например Enter, Escape, Tab.' })}</div>
    <div class="stack gap-3" data-for="fill select">
      <div class="stack gap-1"><span class="t-footnote" id="sf-source-label">Что подставить</span>${segmentedHtml('Что подставить', sourceOptions, valueSource)}</div>
      <div data-source="text">${field({ id: 'sf-value', label: 'Значение', value: step.action === 'fill' || step.action === 'select' ? (step.value || '') : '', required: false, hint: 'Можно вставить параметр: {{имя}}.' })}</div>
      <div data-source="param">${selectField({ id: 'sf-param', label: 'Параметр', options: paramOptions, value: PARAM_REF.exec(step.value || '') ? PARAM_REF.exec(step.value)[1] : '', hint: proc.params.length ? '' : 'Добавьте параметр в карточке «Параметры».' })}</div>
      <div data-source="secret">${secretField}</div>
    </div>
    <div class="stack gap-4" data-for="wait">${field({ id: 'sf-wait', label: 'Пауза, мс', value: step.action === 'wait' ? (step.value || '') : '', inputmode: 'numeric', required: false, hint: 'Можно оставить пустым и указать только элемент выше.' })}</div>
    <fieldset class="proc-fieldset stack gap-3"><legend>Было: что должно быть верно до шага</legend>
      ${field({ id: 'sf-pre-url', label: 'Адрес страницы подходит под шаблон', value: pre.url_matches || '', mono: true, required: false, hint: 'Регулярное выражение, например ^https://example\\.com/login' })}
      <div class="stack gap-3">${selectField({ id: 'sf-pre-role', label: 'Виден элемент', options: roleOptions(vis(pre).role || 'button', { code: false }), value: vis(pre).role || 'button' })}
      ${field({ id: 'sf-pre-name', label: 'Подпись видимого элемента', value: vis(pre).name || '', required: false })}</div>
    </fieldset>
    <fieldset class="proc-fieldset stack gap-3"><legend>Получилось: что должно быть верно после шага</legend>
      ${field({ id: 'sf-exp-url', label: 'Адрес страницы подходит под шаблон', value: exp.url_matches || '', mono: true, required: false })}
      <div class="stack gap-3">${selectField({ id: 'sf-exp-role', label: 'Виден элемент', options: roleOptions(vis(exp).role || 'button', { code: false }), value: vis(exp).role || 'button' })}
      ${field({ id: 'sf-exp-name', label: 'Подпись видимого элемента', value: vis(exp).name || '', required: false })}</div>
      ${field({ id: 'sf-exp-text', label: 'На странице есть текст', value: exp.text || '', required: false })}
      ${field({ id: 'sf-exp-timeout', label: 'Ждать условие, мс', value: exp.timeout_ms || '', inputmode: 'numeric', required: false, hint: 'По умолчанию 5000.' })}
    </fieldset>
    ${checkField({ id: 'sf-retry', label: 'Шаг можно безопасно повторить при сбое (чтение, переход, переключатель)', checked: !!step.safe_to_retry })}
    ${selectField({
    id: 'sf-risk', label: 'Подтверждение', value: step.risk || 'none', hint: riskHint,
    options: RISK_ORDER.map((r) => ({ value: r, label: r === 'none' ? 'Не требуется' : RISK[r].label, disabled: riskRank(r) < riskRank(floor) })),
  })}
    <details class="proc-advanced"><summary>Дополнительно</summary>
      <div class="stack gap-4" style="margin-top:12px;">
        ${field({ id: 'sf-rawrole', label: 'Роль элемента по коду', value: rawRole ? t.role : '', mono: true, required: false, hint: 'Используется, если выше выбрано «Другая, по коду».' })}
        ${field({ id: 'sf-selector', label: 'Селектор CSS (запасной способ найти элемент)', value: t.selector || '', mono: true, required: false, hint: 'Параметры {{имя}} в селекторе не работают. Ввод в поле по селектору всегда идёт с подтверждением.' })}
      </div>
    </details>
    <div class="form-alert" id="sf-alert" aria-live="polite"></div>
    <div class="btn-row btn-row-2"><button type="button" class="btn btn-secondary" data-close>Отмена</button><button type="submit" class="btn btn-primary" id="sf-submit">Сохранить шаг</button></div>
  </form>`;
}

// Сопоставление пути из detail 422 («expect.url_matches») с полем формы шага.
const STEP_PATHS = {
  action: 'sf-action', risk: 'sf-risk', secret_ref: 'sf-secret', safe_to_retry: 'sf-retry',
  'target.url': 'sf-url', 'target.role': 'sf-role', 'target.name': 'sf-name', 'target.selector': 'sf-selector', target: 'sf-name',
  'precondition.url_matches': 'sf-pre-url', 'precondition.visible': 'sf-pre-name', 'precondition.visible.name': 'sf-pre-name', 'precondition.visible.role': 'sf-pre-role',
  'expect.url_matches': 'sf-exp-url', 'expect.visible': 'sf-exp-name', 'expect.visible.name': 'sf-exp-name', 'expect.visible.role': 'sf-exp-role', 'expect.text': 'sf-exp-text', 'expect.timeout_ms': 'sf-exp-timeout',
};

async function openStepDialog({ proc, index, secrets, onSaved }) {
  const isNew = index < 0;
  const base = isNew ? { id: nextStepId(proc.steps), action: 'click', target: { role: 'button', name: '' }, value: null, secret_ref: null, precondition: null, expect: null, safe_to_retry: false, risk: 'none' } : proc.steps[index];
  const secretNames = await secrets();
  const names = secretNames ? secretNames.map((s) => s.name) : null;
  const dlg = openDialog({
    title: isNew ? 'Новый шаг' : `Шаг ${index + 1}`,
    subtitle: isNew ? '' : stepTitle(base),
    content: stepFormHtml(base, proc, names),
  });
  const form = $('#sf-form', dlg.el);
  const el = (id) => $(`#${id}`, dlg.el);
  const action = () => el('sf-action').value;
  const source = () => segmentedValue(dlg.el, 'Что подставить') || 'text';
  wireSegmented(dlg.el);

  function sync() {
    const a = action();
    $$('[data-for]', form).forEach((blk) => { blk.hidden = !blk.getAttribute('data-for').split(' ').includes(a); });
    const secretRadio = $('[role="radio"][data-value="secret"]', form);
    const group = secretRadio.closest('[role="radiogroup"]');
    secretRadio.hidden = a !== 'fill';
    group.style.gridTemplateColumns = `repeat(${a === 'fill' ? 3 : 2},minmax(0,1fr))`;
    if (a !== 'fill' && source() === 'secret') { const textRadio = $('[role="radio"][data-value="text"]', form); textRadio.setAttribute('aria-checked', 'true'); textRadio.tabIndex = 0; secretRadio.setAttribute('aria-checked', 'false'); secretRadio.tabIndex = -1; }
    const s = source();
    $$('[data-source]', form).forEach((blk) => { blk.hidden = blk.getAttribute('data-source') !== s; });
  }
  el('sf-action').addEventListener('change', sync);
  new MutationObserver(sync).observe(form, { subtree: true, attributes: true, attributeFilter: ['aria-checked'] });
  el('sf-role').addEventListener('change', () => { if (el('sf-role').value === CODE_ROLE) $('.proc-advanced', form).open = true; });
  if (base.target && base.target.role && !ROLES[base.target.role]) $('.proc-advanced', form).open = true;
  if (base.target && base.target.selector) $('.proc-advanced', form).open = true;
  sync();
  // Блоки скрыты после sync(): фокус на первом поле, которое осталось видимым.
  el('sf-action').focus();

  const fieldErrors = {};
  const fail = (id, text) => { fieldErrors[id] = fieldErrors[id] || text; };

  function readStep() {
    for (const k of Object.keys(fieldErrors)) delete fieldErrors[k];
    const a = action();
    const step = { ...cleanStep(base), action: a };
    const selector = el('sf-selector').value.trim();
    const roleValue = el('sf-role').value;
    const role = roleValue === CODE_ROLE ? el('sf-rawrole').value.trim() : roleValue;
    const name = el('sf-name').value.trim();
    if (a === 'navigate') {
      const url = el('sf-url').value.trim();
      if (!url) fail('sf-url', 'Укажите адрес страницы');
      step.target = { url };
      step.value = null;
      step.secret_ref = null;
    } else if (a === 'press') {
      const key = el('sf-key').value.trim();
      if (!key) fail('sf-key', 'Укажите клавишу');
      step.target = null;
      step.value = key;
      step.secret_ref = null;
    } else {
      const target = {};
      if (role && (name || !selector)) target.role = role;
      if (name) target.name = name;
      if (selector) target.selector = selector;
      const needsTarget = a === 'click' || a === 'fill' || a === 'select';
      if (needsTarget && !name && !selector) fail('sf-name', 'Укажите подпись элемента или селектор в «Дополнительно»');
      if (roleValue === CODE_ROLE && !role) fail('sf-rawrole', 'Укажите роль по коду или выберите другую');
      step.target = Object.keys(target).length ? target : null;
      if (a === 'fill' || a === 'select') {
        const s = source();
        if (s === 'text') {
          const v = el('sf-value').value;
          step.value = v === '' ? null : v;
          step.secret_ref = null;
          if (step.value == null && !base.needs_value) fail('sf-value', 'Укажите значение, параметр или секрет');
        } else if (s === 'param') {
          const prm = el('sf-param').value;
          step.value = prm ? `{{${prm}}}` : null;
          step.secret_ref = null;
          if (!prm) fail('sf-param', 'Выберите параметр');
        } else {
          const nameVal = el('sf-secret').value.trim();
          step.value = null;
          step.secret_ref = nameVal ? `vault:${nameVal}` : null;
          if (!nameVal) fail('sf-secret', 'Выберите секрет');
        }
      } else if (a === 'wait') {
        const ms = el('sf-wait').value.trim();
        if (ms && !/^\d+$/.test(ms)) fail('sf-wait', 'Нужно целое число миллисекунд');
        step.value = ms || null;
        step.secret_ref = null;
        if (!ms && !step.target) fail('sf-wait', 'Укажите паузу или элемент, которого нужно дождаться');
      } else {
        step.value = null;
        step.secret_ref = null;
        if (a === 'assert' && !step.target) fail('sf-name', 'Укажите, что проверить: подпись элемента или селектор');
      }
    }
    const cond = (prefix, withExtra) => {
      const out = {};
      const url = el(`sf-${prefix}-url`).value.trim();
      if (url) out.url_matches = url;
      const vname = el(`sf-${prefix}-name`).value.trim();
      if (vname) out.visible = { role: el(`sf-${prefix}-role`).value, name: vname };
      if (withExtra) {
        const text = el('sf-exp-text').value.trim();
        if (text) out.text = text;
        const timeout = el('sf-exp-timeout').value.trim();
        if (timeout) {
          if (!/^\d+$/.test(timeout) || Number(timeout) <= 0) fail('sf-exp-timeout', 'Нужно положительное целое число миллисекунд');
          else out.timeout_ms = Number(timeout);
        }
      }
      return Object.keys(out).length ? out : null;
    };
    step.precondition = cond('pre', false);
    step.expect = cond('exp', true);
    step.safe_to_retry = el('sf-retry').checked;
    // Риск вычисляет ядро по итоговой цели шага. Не тронутый выбор не отправляется: после смены подписи, роли или
    // действия нижняя граница у ядра другая, а форма знает только computed_risk сохранённого шага. Выбранное вручную
    // (выше нижней границы или в обход формы) уходит как есть, ядро проверит его ещё раз.
    const chosen = el('sf-risk').value;
    if (chosen === (base.risk || 'none') && riskRank(chosen) <= riskRank(riskFloor(base))) delete step.risk;
    else step.risk = chosen;
    if (step.value != null || step.secret_ref) { delete step.needs_value; delete step.needs_secret; }
    return step;
  }

  function applyErrors() {
    let first = null;
    for (const [id, text] of Object.entries(fieldErrors)) {
      const input = el(id);
      if (!input) continue;
      setFieldError(input, text);
      if (!first || input.compareDocumentPosition(first) & Node.DOCUMENT_POSITION_FOLLOWING) first = input;
    }
    if (first) {
      const adv = first.closest('.proc-advanced');
      if (adv) adv.open = true;
      first.focus();
    }
    return !!first;
  }

  function mapServerError(err) {
    const { path, message } = parseDetail(err.detail);
    const m = /^steps\.(\d+)\.(.+)$/.exec(path);
    if (!m) return null;
    const stepIndex = Number(m[1]);
    const mine = isNew ? proc.steps.length : index;
    if (stepIndex !== mine) return { title: `Ошибка в шаге ${stepIndex + 1}`, text: message };
    let id = STEP_PATHS[m[2]];
    if (m[2] === 'value') id = a_valueField();
    if (!id) return { title: 'Шаг не принят', text: message };
    return { id, text: message };
  }
  function a_valueField() {
    const a = action();
    if (a === 'press') return 'sf-key';
    if (a === 'wait') return 'sf-wait';
    if (a === 'navigate') return 'sf-url';
    const s = source();
    return s === 'param' ? 'sf-param' : s === 'secret' ? 'sf-secret' : 'sf-value';
  }

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    clearErrors(form);
    const step = readStep();
    if (applyErrors()) return;
    const steps = proc.steps.map(cleanStep);
    if (isNew) steps.push(step); else steps[index] = step;
    const button = $('#sf-submit', dlg.el);
    setBusy(button, true, 'Сохраняю', 'Сохранить шаг');
    try {
      const saved = await api.patchProcedure(proc.id, { steps });
      dlg.close();
      onSaved(saved);
    } catch (err) {
      setBusy(button, false, '', 'Сохранить шаг');
      if (err.status === 422 || err.status === 400) {
        const mapped = mapServerError(err);
        if (mapped && mapped.id && el(mapped.id)) {
          const input = el(mapped.id);
          setFieldError(input, mapped.text);
          const adv = input.closest('.proc-advanced');
          if (adv) adv.open = true;
          input.focus();
          return;
        }
        if (mapped) { setAlert($('#sf-alert', dlg.el), mapped.title, mapped.text); return; }
      }
      const f = procFailure(err, 'Шаг не сохранён. ');
      setAlert($('#sf-alert', dlg.el), f.title, f.text);
    }
  });
}

// ---------------------------------------------------------------------------
// Запуск: лист с выбором бота и значений параметров
// ---------------------------------------------------------------------------
async function openRunSheet({ proc, bots, secrets }) {
  const secretList = await secrets();
  const names = secretList ? secretList.map((s) => s.name) : null;
  const botOptions = [...(proc.bot_id && bots.some((b) => b.id === proc.bot_id) ? [] : [{ value: '', label: 'Выберите бота' }]), ...bots.map((b) => ({ value: b.id, label: b.name }))];
  const confirms = stepsToConfirm(proc.steps);
  const paramField = (prm, i) => {
    const id = `rs-p-${i}`;
    const req = prm.required ? 'обязательный' : 'необязательный';
    if (prm.secret) {
      return names
        ? selectField({ id, label: `${prm.name} (секрет, ${req})`, options: [{ value: '', label: 'Выберите секрет' }, ...names.map((n) => ({ value: `vault:${n}`, label: n }))], value: '', hint: 'Значение остаётся в хранилище: передаётся только ссылка.' })
        : field({ id, label: `${prm.name} (секрет, ${req}): имя секрета`, mono: true, required: prm.required, hint: 'Список секретов недоступен: введите имя секрета, не пароль.' });
    }
    const label = `${prm.name} (${req}${prm.default != null && prm.default !== '' ? `, по умолчанию «${prm.default}»` : ''})`;
    if (prm.type === 'boolean') {
      return selectField({ id, label, options: [{ value: '', label: prm.default != null && prm.default !== '' ? 'По умолчанию' : 'Не задано' }, { value: 'true', label: 'Да' }, { value: 'false', label: 'Нет' }], value: '' });
    }
    return field({ id, label, inputmode: prm.type === 'number' ? 'decimal' : '', required: prm.required, placeholder: prm.default != null ? String(prm.default) : '' });
  };
  const dlg = openDialog({
    title: 'Запустить процедуру',
    subtitle: proc.name,
    content: `<form class="stack gap-4" id="rs-form" novalidate>
      ${selectField({ id: 'rs-bot', label: 'Бот', options: botOptions, value: proc.bot_id && bots.some((b) => b.id === proc.bot_id) ? proc.bot_id : '', hint: 'Запуск идёт в новом треде этого бота.' })}
      ${proc.params.map(paramField).join('')}
      ${confirms.length ? `<div class="banner banner-attention" role="note"><span class="banner-icon">${ICONS.alert}</span><span class="banner-text"><span class="banner-title">${confirms.length} ${plural(confirms.length, 'шаг потребует', 'шага потребуют', 'шагов потребуют')} вашего подтверждения</span>
        <ul class="proc-confirm-list">${confirms.map((x) => `<li>Шаг ${x.n}: ${esc(stepTitle(x.step))} (${esc(riskLabel(x.step.risk).toLowerCase())})</li>`).join('')}</ul></span></div>` : ''}
      <div class="form-alert" id="rs-alert" aria-live="polite"></div>
      <div class="btn-row btn-row-2"><button type="button" class="btn btn-secondary" data-close>Отмена</button><button type="submit" class="btn btn-primary" id="rs-submit">${ICONS.play}Запустить</button></div>
    </form>`,
  });
  const form = $('#rs-form', dlg.el);
  const idle = `${ICONS.play}Запустить`;
  ($('#rs-p-0', dlg.el) || $('#rs-submit', dlg.el)).focus();
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    clearErrors(form);
    const bot = $('#rs-bot', dlg.el);
    const errors = [];
    if (!bot.value) errors.push([bot, 'Выберите бота']);
    const values = {};
    proc.params.forEach((prm, i) => {
      const input = $(`#rs-p-${i}`, dlg.el);
      let v = input.value.trim();
      const hasDefault = prm.default != null && prm.default !== '';
      if (v === '') {
        if (prm.required && !hasDefault) errors.push([input, prm.secret ? 'Выберите секрет' : 'Укажите значение']);
        return;
      }
      if (prm.secret) { values[prm.name] = v.startsWith('vault:') ? v : `vault:${v}`; return; }
      if (prm.type === 'number') {
        if (!Number.isFinite(Number(v.replace(',', '.')))) { errors.push([input, 'Нужно число']); return; }
        v = Number(v.replace(',', '.'));
      } else if (prm.type === 'boolean') v = v === 'true';
      values[prm.name] = v;
    });
    if (errors.length) {
      errors.forEach(([input, text]) => setFieldError(input, text));
      errors[0][0].focus();
      return;
    }
    const button = $('#rs-submit', dlg.el);
    setBusy(button, true, 'Запускаю', idle);
    try {
      const run = await api.runProcedure(proc.id, { bot_id: bot.value, params: values });
      dlg.close();
      location.hash = runHash(run.id);
    } catch (err) {
      setBusy(button, false, '', idle);
      const alertBox = $('#rs-alert', dlg.el);
      if (err.status === 501 || err.code === 'not_implemented') { setAlert(alertBox, NOT_ENABLED.title, NOT_ENABLED.text); return; }
      if (err.status === 400 || err.status === 422) {
        const { path, message } = parseDetail(err.detail);
        const pm = /^params\.(.+)$/.exec(path);
        const idx = pm ? proc.params.findIndex((x) => x.name === pm[1]) : -1;
        if (idx >= 0) { const input = $(`#rs-p-${idx}`, dlg.el); setFieldError(input, message); input.focus(); return; }
        if (path === 'bot_id') { setFieldError(bot, message); bot.focus(); return; }
      }
      if (err.status === 409) { setAlert(alertBox, 'Запуск не принят', err.detail || 'Состояние процедуры изменилось: обновите экран.'); return; }
      const f = procFailure(err, 'Запуск не начался. ');
      setAlert(alertBox, f.title, f.text);
    }
  });
}

// ---------------------------------------------------------------------------
// Выполнение: статус запуска и шаги. Привязанный к треду запуск обновляется по потоку треда, остальные опросом раз в 2 с.
// ---------------------------------------------------------------------------
const POLL_MS = 2000;
const STREAM_FALLBACK_MS = 10000;

async function viewRun(id) {
  const c = context();
  let run; let proc; let bots;
  try {
    run = await api.getProcedureRun(id);
    [proc, bots] = await Promise.all([api.getProcedure(run.procedure_id), api.listBots().catch(() => [])]);
  } catch (err) {
    if (err.status === 401) return;
    await c.frame({
      title: 'Запуск', subtitle: 'Процедуры', backHref: HASH_LIST, activeNav: 'routines',
      body: `<div class="page-narrow">${err.status === 404
        ? stateHtml({ iconHtml: ICONS.checklist, title: 'Запуск не найден', text: 'Возможно, процедуру удалили вместе с историей.', actions: `<a class="btn btn-secondary" href="${HASH_LIST}">К процедурам</a>` })
        : loadFailure(err, 'Запуск')}</div>`,
    });
    $('[data-act="retry"]')?.addEventListener('click', () => c.rerender());
    return;
  }
  const botName = (bots.find((b) => b.id === run.bot_id) || {}).name || 'бот';
  await c.frame({
    title: proc.name,
    subtitle: `Запуск · ${botName} · ${fmtDateTime(run.created_at)}`,
    backHref: procHash(proc.id),
    activeNav: 'routines',
    body: '<div class="page-narrow stack gap-4" id="rn-root"></div>',
  });
  const root = $('#rn-root');
  let closed = false;
  let timer = null;
  let stream = null;
  let debounce = null;
  let inflight = false;
  let approvalId = null;
  let browserState = null;
  let refreshError = false;

  // Шаги рисуем по снимку запуска (run.steps): правка процедуры после запуска его не меняет. У запуска без снимка
  // (старые записи) берутся нынешние шаги процедуры.
  const hasSnapshot = () => Array.isArray(run.steps) && run.steps.length > 0;
  const runSteps = () => (hasSnapshot() ? run.steps : proc.steps);

  function statusBlock() {
    const st = RUN_STATUS[run.status] || { text: run.status, kind: 'neutral' };
    const idx = run.next_step;
    const step = runSteps()[idx];
    const stepName = step ? `шаг ${idx + 1} «${stepTitle(step)}»` : 'шаг';
    let detail = '';
    let actions = '';
    if (run.status === 'queued') detail = 'Запуск стоит в очереди и скоро начнётся.';
    else if (run.status === 'running') detail = step ? `Выполняется ${stepName}.` : 'Выполняется.';
    else if (run.status === 'waiting_approval') {
      detail = `Для ${stepName} нужно ваше подтверждение.`;
      actions = `<a class="btn btn-attention" href="${approvalId ? `#/approvals/${encodeURIComponent(approvalId)}` : `#/threads/${encodeURIComponent(run.thread_id || '')}`}" data-link="approval">Открыть подтверждение</a>`;
    } else if (run.status === 'waiting_model') {
      detail = `Не нашёлся элемент или страница не та, что ожидалась (${stepName}). Бот разбирается и предложит правку шага, она применится после вашего подтверждения.`;
      if (run.thread_id) actions = `<a class="btn btn-secondary" href="#/threads/${encodeURIComponent(run.thread_id)}">Открыть тред бота</a>`;
    } else if (run.status === 'waiting_human') {
      const human = browserState === 'human';
      detail = human
        ? `Вы управляете браузером бота. Когда вернёте управление, запуск продолжится с шага ${idx + 1} (${stepName}) с повторной проверкой условия «Было».`
        : `Исход ${stepName} неизвестен, а повторять его само сервер не вправе: действие могло выполниться наполовину. Проверьте страницу в браузере бота и решите, что делать.`;
      // Три решения владельца: повторить шаг, пропустить его или остановить запуск. Пока браузером управляет человек,
      // повтор и пропуск не предлагаются: сначала нужно вернуть управление боту.
      actions = `${human ? '<button type="button" class="btn btn-primary" data-act="return-browser">Вернуть управление боту</button>'
        : '<button type="button" class="btn btn-primary" data-act="decide-retry">Повторить шаг</button><button type="button" class="btn btn-secondary" data-act="decide-skip">Пропустить шаг</button>'}<button type="button" class="btn btn-danger" data-act="decide-stop">Остановить запуск</button><a class="btn btn-secondary" href="#/bots/${encodeURIComponent(run.bot_id)}/browser">Открыть браузер бота</a>`;
    } else if (run.status === 'failed') {
      detail = runErrorText(run.error) || 'Запуск завершился ошибкой.';
    } else if (run.status === 'done') detail = 'Все шаги выполнены.';
    else if (run.status === 'stopped') detail = 'Запуск остановлен. Выполненные шаги остались выполненными.';
    const dur = runDuration(run);
    const stop = isActiveRun(run) && run.status !== 'waiting_human' ? '<button type="button" class="btn btn-danger" data-act="stop">Остановить</button>' : '';
    return `<section class="card card-pad stack gap-3" aria-labelledby="rn-status-h">
      <div class="row gap-2"><h2 class="t-headline flex-1" id="rn-status-h">Статус</h2>${dur ? `<span class="t-footnote" data-duration>${esc(dur)}</span>` : ''}</div>
      <div class="stack gap-2" role="status" aria-live="polite" data-run-status="${esc(run.status)}">
        <span class="status-line t-body"><span class="status-dot ${st.kind}"></span><strong>${esc(st.text)}</strong></span>
        ${detail ? `<span class="t-body" data-run-detail>${esc(detail)}</span>` : ''}
      </div>
      ${refreshError ? '<p class="t-footnote" style="margin:0;color:var(--attention-text);">Не удалось обновить статус, пробую ещё раз.</p>' : ''}
      ${actions || stop ? `<div class="row gap-2 wrap-row">${actions}${stop}</div>` : ''}
    </section>`;
  }

  function stepsBlock() {
    const shown = runSteps();
    const states = stepStates(run, shown);
    const changed = proc.version !== run.procedure_version;
    const note = !changed ? '' : hasSnapshot()
      ? `Запуск шёл по версии ${run.procedure_version}, сейчас у процедуры версия ${proc.version}: показаны шаги версии ${run.procedure_version}.`
      : `Запуск шёл по версии ${run.procedure_version}, сейчас у процедуры версия ${proc.version}: показаны её шаги, журнал сопоставлен по номерам шагов.`;
    return `<section class="card card-pad stack gap-3" aria-labelledby="rn-steps-h">
      <h2 class="t-headline" id="rn-steps-h">Шаги</h2>
      ${note ? `<p class="t-footnote" style="margin:0;" data-run-version-note>${esc(note)}</p>` : ''}
      <ol class="proc-steps">${shown.map((s, i) => {
    const { state, entry } = states[i];
    const dur = entry && entry.duration_ms != null ? fmtDuration(entry.duration_ms) : '';
    const err = entry && entry.error ? runErrorText(entry.error) : (state === 'error' && !entry && run.error ? runErrorText(run.error) : '');
    return `<li class="proc-step run-step" data-step-id="${esc(s.id)}" data-state="${state}">
          <div class="proc-step-main">
            <span class="step-num" aria-hidden="true">${i + 1}</span>
            <div class="stack gap-1 flex-1 min-w-0">
              <span class="t-body proc-step-title"><span class="sr-only">Шаг ${i + 1}: </span>${esc(stepTitle(s))}</span>
              <span class="proc-badges"><span class="run-step-state">${STEP_STATE_ICON[state] || ''}<span data-step-state>${esc(STEP_STATE[state])}</span></span>${dur ? `<span class="t-footnote">${esc(dur)}</span>` : ''}${riskBadge(s)}</span>
              ${err ? `<span class="t-footnote" style="color:var(--danger-fg);">${esc(err)}</span>` : ''}
            </div>
          </div>
        </li>`;
  }).join('')}</ol>
    </section>`;
  }

  function paramsBlock() {
    const entries = Object.entries(run.params || {});
    if (!entries.length) return '';
    const show = (name, v) => {
      const prm = proc.params.find((p) => p.name === name);
      if (prm && prm.secret) return `секрет «${secretName(v)}»`;
      if (typeof v === 'string' && v.startsWith('vault:')) return `секрет «${secretName(v)}»`;
      return String(v);
    };
    return `<section class="card card-pad stack gap-2" aria-labelledby="rn-params-h"><h2 class="t-headline" id="rn-params-h">Параметры запуска</h2>
      <dl class="proc-kvs">${entries.map(([k, v]) => `<div class="proc-kv"><dt>${esc(k)}</dt><dd>${esc(show(k, v))}</dd></div>`).join('')}</dl></section>`;
  }

  function paint() {
    const active = document.activeElement;
    const key = active && root.contains(active) ? (active.getAttribute('data-act') || active.getAttribute('data-link')) : null;
    root.innerHTML = `<div class="form-alert" id="rn-alert" aria-live="polite"></div>${statusBlock()}${stepsBlock()}${paramsBlock()}
      <div class="row gap-2 wrap-row">
        <a class="btn btn-secondary" href="${procHash(proc.id)}">К процедуре</a>
        ${run.thread_id ? `<a class="btn btn-secondary" href="#/threads/${encodeURIComponent(run.thread_id)}">Тред бота</a>` : ''}
      </div>`;
    if (key) $(`[data-act="${key}"], [data-link="${key}"]`, root)?.focus();
  }

  async function sideData() {
    if (run.status === 'waiting_approval') {
      try {
        const mine = (await api.listApprovals('pending')).filter((a) => a.thread_id === run.thread_id);
        const hit = (run.turn_id && mine.find((a) => a.turn_id === run.turn_id)) || mine[mine.length - 1];
        approvalId = hit ? hit.id : null;
      } catch { approvalId = null; }
    } else approvalId = null;
    if (run.status === 'waiting_human') {
      try { browserState = (await api.getBrowser(run.bot_id)).state; } catch { browserState = null; }
    } else browserState = null;
  }

  function schedule() {
    clearTimeout(timer);
    if (closed || !isActiveRun(run)) { closeStream(); return; }
    timer = setTimeout(refresh, stream ? STREAM_FALLBACK_MS : POLL_MS);
  }
  function closeStream() { if (stream) { stream(); stream = null; } }

  async function refresh() {
    if (closed || inflight) return;
    inflight = true;
    try {
      const fresh = await api.getProcedureRun(id);
      refreshError = false;
      if (fresh.procedure_version !== run.procedure_version || fresh.status !== run.status) {
        try { proc = await api.getProcedure(fresh.procedure_id); } catch { /* остаются прежние шаги */ }
      }
      run = fresh;
      await sideData();
    } catch (err) {
      if (err.status === 401) { closed = true; return; }
      refreshError = true;
    } finally { inflight = false; }
    if (closed) return;
    paint();
    schedule();
  }
  const refreshSoon = () => { clearTimeout(debounce); debounce = setTimeout(refresh, 250); };

  root.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-act]');
    if (!btn || btn.disabled) return;
    const act = btn.getAttribute('data-act');
    if (act === 'stop') {
      confirmAction({
        title: 'Остановить запуск?',
        subtitle: proc.name,
        text: 'Выполненные шаги останутся выполненными, откатить их нельзя. Остановленный запуск не продолжится сам.',
        confirmLabel: 'Остановить',
        run: () => api.stopProcedureRun(id),
        describeError: (err) => (err.status === 409
          ? { title: 'Запуск уже завершён', text: 'Остановить нечего: экран обновится.' }
          : procFailure(err, 'Запуск не остановлен. ')),
        done: refresh,
      });
    } else if (act === 'decide-retry' || act === 'decide-skip' || act === 'decide-stop') {
      const action = act.slice('decide-'.length);
      const idx = run.next_step;
      const step = runSteps()[idx];
      const stepName = step ? `шаг ${idx + 1} «${stepTitle(step)}»` : 'шаг';
      const texts = {
        retry: { title: 'Повторить шаг?', label: 'Повторить', text: `Сервер выполнит ${stepName} ещё раз. Если он уже сработал наполовину (например, отправил форму), действие может повториться.` },
        skip: { title: 'Пропустить шаг?', label: 'Пропустить', text: `${stepName[0].toUpperCase()}${stepName.slice(1)} не выполнится, запуск продолжится со следующего. Если страница не в том состоянии, следующие шаги могут не сработать.` },
        stop: { title: 'Остановить запуск?', label: 'Остановить', text: 'Выполненные шаги останутся выполненными, откатить их нельзя. Остановленный запуск не продолжится сам.' },
      }[action];
      confirmAction({
        title: texts.title,
        subtitle: proc.name,
        text: texts.text,
        confirmLabel: texts.label,
        run: () => api.decideProcedureRun(id, action),
        describeError: (err) => (err.status === 409
          ? { title: 'Запуск уже не ждёт решения', text: 'Экран обновится.' }
          : procFailure(err, 'Решение не принято. ')),
        done: refresh,
      });
    } else if (act === 'return-browser') {
      btn.disabled = true;
      api.browserReturn(run.bot_id).then(refresh, (err) => {
        btn.disabled = false;
        const f = procFailure(err, 'Управление не возвращено. ');
        setAlert($('#rn-alert', root), f.title, f.text);
      });
    }
  });

  await sideData();
  paint();
  if (isActiveRun(run) && run.thread_id) stream = openThreadStream(run.thread_id, 0, refreshSoon);
  schedule();
  c.setCleanup(() => { closed = true; clearTimeout(timer); clearTimeout(debounce); closeStream(); });
}
