// api.js: HTTP-клиент ядра botstead. Сессия по cookie + X-CSRF; OWNER_TOKEN из localStorage оставлен как устаревший
// способ входа. ?mock=1 подменяет ответы фейковыми данными (&role=member, &auth=none, &setup=1 для e2e).
import { AVATAR_KINDS } from './avatars.js';

const TOKEN_KEY = 'bothub_owner_token';
const params = new URLSearchParams(location.search);
export const MOCK = params.get('mock') === '1';

export function getToken() {
  return localStorage.getItem(TOKEN_KEY) || '';
}
export function setToken(v) {
  if (v) localStorage.setItem(TOKEN_KEY, v);
  else localStorage.removeItem(TOKEN_KEY);
}

export class ApiError extends Error {
  constructor(status, body) {
    super((body && body.detail) || (body && body.error) || `HTTP ${status}`);
    this.status = status;
    this.code = body && body.error;
    this.detail = body && body.detail;
  }
}

// CSRF-токен текущей cookie-сессии: приходит с логином и с /auth/me, нужен для POST, PATCH и DELETE.
let csrfToken = '';
export function forgetCsrf() { csrfToken = ''; }

// Ответ 401 вне /auth/* и /setup значит, что сессия закрыта: приложение показывает вход.
function notifyUnauthorized() {
  window.dispatchEvent(new CustomEvent('bothub-unauthorized'));
}

async function request(path, { method = 'GET', body, isForm, bearer = true, headers: extraHeaders = {} } = {}) {
  const headers = { ...extraHeaders };
  const token = bearer ? getToken() : '';
  if (token) headers['Authorization'] = `Bearer ${token}`;
  const publicPost = method === 'POST' && ['/setup', '/auth/login', '/invites/accept'].includes(path);
  if (!token && ['POST', 'PATCH', 'DELETE'].includes(method) && !publicPost) {
    if (!csrfToken) {
      const me = await fetch('api/auth/me', { credentials: 'same-origin' });
      if (!me.ok) {
        let detail = null;
        try { detail = await me.json(); } catch { /* empty response */ }
        if (me.status === 401) notifyUnauthorized();
        throw new ApiError(me.status, detail);
      }
      csrfToken = (await me.json()).csrf_token || '';
    }
    if (csrfToken) headers['X-CSRF'] = csrfToken;
  }
  let payload = body;
  if (body && !isForm) {
    headers['Content-Type'] = 'application/json';
    payload = JSON.stringify(body);
  }
  const res = await fetch(`api${path}`, { method, headers, body: payload, credentials: 'same-origin' });
  let data = null;
  const text = await res.text();
  if (text) {
    try { data = JSON.parse(text); } catch { data = text; }
  }
  if (!res.ok) {
    if (res.status === 401 && !path.startsWith('/auth/') && path !== '/setup') notifyUnauthorized();
    throw new ApiError(res.status, data);
  }
  return data;
}

// ---------------------------------------------------------------------------
// Мок-режим: фейковые данные для превью без бэкенда (?mock=1)
// ---------------------------------------------------------------------------

const now = Date.now();
const minutesAgo = (m) => new Date(now - m * 60000).toISOString();
const minutesAhead = (m) => new Date(now + m * 60000).toISOString();

const MOCK_DEFAULT_MODEL = { claude: 'Sonnet 5', codex: 'GPT-6 Sol', gemini: 'Gemini 3.1 Pro' };

const mockBots = [
  {
    id: 'scout', name: 'Скаут', role: 'Ищет вакансии и заявки', avatar: 'scout',
    provider: 'gemini', model: 'Gemini 3.1 Pro', executor: 'container', mac_full_control: false,
    status: 'waiting', location: 'Сервер',
    summary: 'Нашёл 7 вакансий SRE, жду решения по отклику',
    status_label: 'Ждёт решения', status_kind: 'attention',
    auto_allow: [{ tool: 'search.*' }],
    budget_daily_tokens: 200000,
  },
  {
    id: 'mac', name: 'Мак', role: 'Ищет файлы, открывает приложения, делает скриншоты', avatar: 'mac',
    provider: 'claude', model: 'Sonnet 5', executor: 'mac', mac_full_control: true,
    status: 'idle', location: 'Mac',
    summary: 'Нашёл 3 файла: договор аренды 2025',
    status_label: 'Готово', status_kind: 'success',
    auto_allow: [{ tool: 'mac_find_files' }, { tool: 'mac_read_file' }],
    budget_daily_tokens: 200000,
  },
  {
    id: 'sre', name: 'SRE', role: 'Диагностика и инциденты на серверах', avatar: 'sre',
    provider: 'claude', model: 'Opus 5.5', executor: 'container', mac_full_control: false,
    status: 'running', location: 'Сервер',
    summary: 'Инцидент: 502 на webapp, 2 гипотезы',
    status_label: 'Работает', status_kind: 'success',
    auto_allow: [{ tool: 'shell', match: { cmd: 'docker ps*' } }],
    budget_daily_tokens: 200000,
  },
  {
    id: 'coder', name: 'Кодер', role: 'Пишет и правит код на своём компьютере', avatar: 'coder',
    provider: 'codex', model: 'GPT-6 Sol', executor: 'container', mac_full_control: false,
    status: 'error', location: 'Сервер',
    summary: 'Остановлен предохранителем: 3 одинаковые ошибки',
    status_label: 'Стоп', status_kind: 'danger',
    auto_allow: [],
    budget_daily_tokens: 200000,
  },
  {
    id: 'archive', name: 'Архив', role: 'Раскладывает файлы и сканы по папкам', avatar: 'archive',
    provider: 'gemini', model: 'Gemini 3.8 Flash', executor: 'mac', mac_full_control: true,
    status: 'idle', location: 'Mac',
    summary: 'Ждёт файлы в ~/BotHub/inbox',
    status_label: 'Ждёт события', status_kind: 'neutral',
    auto_allow: [],
    budget_daily_tokens: 200000,
  },
];

// ---------------------------------------------------------------------------
// Мок реестра провайдеров и моделей (docs/contracts.md §11–12). Секреты не хранятся даже в моке: только has_secret.
// Параметры адреса: &providers=none (провайдеров нет), &providers=fail (список не загрузился с первого раза),
// &providers=slow (долгая загрузка), &providers=private (три провайдера с внутренним адресом: ждёт одобрения,
// нужно повторное одобрение, одобрен; два бота на первых двух), &bots=states (боты в статусах no_model,
// error_starting, need_restart и бот без истории для удаления), &login=busy|forbidden|revoked|lost|timeout|start
// (сбои терминала входа), &requests=1 (три запроса админу на внутренние адреса), &requests=stale (то же, первое
// одобрение отвечает 409), &requests=fail (список запросов не загрузился с первого раза).
// Проверка ключа до сохранения (§11): ключ с «bad» даёт 422 key_rejected, с «offline» 422 unreachable, с «weird»
// 422 incompatible, с «limit» 429 rate_limited. Адрес с «down» даёт unreachable, с «notapi» incompatible,
// localhost, 127.*, 169.254.* всегда запрещены (422 invalid_base_url), 192.168.*, 10.*, 172.16.*, *.lan и *.local
// уходят на одобрение администратору (pending_admin). Вызовы создания, правки и одобрения пишутся в
// window.__providerCalls: {name, id, body}. &probe=slow растягивает создание и правку с ключом до 2,5 с.
const MOCK_PROVIDERS_MODE = params.get('providers') || '';
const MOCK_LOGIN_MODE = params.get('login') || '';
// Экран входа ждёт ссылку (и код устройства у codex) 60 с. В моке срок можно сократить: &login_timeout=<мс>.
export const MOCK_LOGIN_TIMEOUT_MS = MOCK ? Number(params.get('login_timeout')) || 0 : 0;
const MOCK_RUNNER = { anthropic_api: 'claude', openai_api: 'codex', openai_compatible: 'codex', google_api: 'gemini' };
const MOCK_CLI_RUNNER = { claude: 'claude', codex: 'codex', agy: 'gemini' };
const MOCK_VENDOR_MODELS = {
  anthropic_api: ['claude-opus-5-5', 'claude-sonnet-5', 'claude-haiku-4-5-20251001'],
  openai_api: ['gpt-5.4', 'gpt-5.4-mini'],
  google_api: ['gemini-3.1-pro-preview', 'gemini-3.1-flash-lite-preview'],
  openai_compatible: ['llama3.3', 'qwen3'],
  cli_claude: ['claude-sonnet-5', 'claude-opus-5-5', 'claude-haiku-4-5-20251001'],
  cli_codex: ['gpt-5.4'],
  cli_agy: ['gemini-3.1-pro-preview', 'gemini-3.1-flash-lite-preview'],
};
let mockModelSeq = 0;
const mockModel = (provider_id, name, extra = {}) => ({ id: `m-${++mockModelSeq}`, provider_id, name, display_name: '', enabled: true, manually_disabled: false, context_window: null, ...extra });
const mockProvider = (id, kind, name, extra = {}) => ({ id, kind, cli: null, name, base_url: null, status: 'ok', last_check_at: minutesAgo(5), last_error: null, has_secret: kind !== 'cli_subscription', secret_tail: null, allow_private: false, created_at: minutesAgo(60 * 24 * 9), ...extra });

const mockProviders = MOCK_PROVIDERS_MODE === 'none' ? [] : [
  mockProvider('p-claude', 'cli_subscription', 'Claude Code', { cli: 'claude', _loggedIn: true }),
  mockProvider('p-anthropic', 'anthropic_api', 'Anthropic API', { secret_tail: 'x9Qz' }),
  mockProvider('p-openai', 'openai_api', 'OpenAI API', { status: 'error', last_error: 'provider check failed', last_check_at: minutesAgo(40), _keyBad: true }),
  mockProvider('p-ollama', 'openai_compatible', 'Ollama дома', { base_url: 'https://ollama.example.org/v1', status: 'error', last_error: 'ConnectError', last_check_at: minutesAgo(180), _down: true }),
  mockProvider('p-codex', 'cli_subscription', 'Codex', { cli: 'codex', status: 'new', last_check_at: null }),
  mockProvider('p-agy', 'cli_subscription', 'Antigravity', { cli: 'agy', _loggedIn: true }),
];
// Провайдеры с внутренним адресом (&providers=private): ждёт одобрения, нужно повторное одобрение, одобрен админом.
if (MOCK_PROVIDERS_MODE === 'private') {
  mockProviders.push(
    mockProvider('p-lan', 'openai_compatible', 'Модель в офисе', { base_url: 'https://llm.office.lan/v1', status: 'pending_admin', last_check_at: null, secret_tail: 'n7Pw' }),
    mockProvider('p-lan-re', 'openai_compatible', 'NAS с моделями', { base_url: 'https://nas.lan/v1', status: 'pending_admin', last_error: 'адрес изменился, нужно повторное одобрение', allow_private: true, allow_private_ips: ['10.0.0.5'], _reapproval: true }),
    mockProvider('p-lan-ok', 'openai_compatible', 'Домашний Ollama', { base_url: 'https://ollama.lan/v1', allow_private: true, allow_private_ips: ['192.168.1.20'], secret_tail: 'abcd' }),
  );
}
const mockModels = MOCK_PROVIDERS_MODE === 'none' ? [] : [
  mockModel('p-claude', 'claude-sonnet-5'), mockModel('p-claude', 'claude-opus-5-5'), mockModel('p-claude', 'claude-haiku-4-5-20251001'),
  mockModel('p-anthropic', 'claude-opus-5-5', { context_window: 200000 }),
  mockModel('p-anthropic', 'claude-sonnet-5', { context_window: 200000 }),
  mockModel('p-anthropic', 'claude-haiku-5', { enabled: false, manually_disabled: true, context_window: 200000 }),
  mockModel('p-anthropic', 'claude-opus-5', { enabled: false, context_window: 200000 }),
  mockModel('p-openai', 'gpt-5.4', { enabled: false }), mockModel('p-openai', 'gpt-5.4-mini', { enabled: false }),
  mockModel('p-ollama', 'llama3.3'), mockModel('p-ollama', 'qwen3'),
  mockModel('p-agy', 'gemini-3.1-pro-preview'), mockModel('p-agy', 'gemini-3.1-flash-lite-preview'),
];

if (MOCK_PROVIDERS_MODE === 'private') {
  mockModels.push(mockModel('p-lan', 'llama3.3'), mockModel('p-lan-re', 'qwen3'), mockModel('p-lan-ok', 'llama3.3'));
}

// Привязка ботов к реестру: как у настоящего ядра, provider/model дублируют выбранную модель (runner-провайдер и имя).
const MOCK_BINDINGS = { scout: ['p-agy', 'gemini-3.1-pro-preview'], mac: ['p-claude', 'claude-sonnet-5'], sre: ['p-claude', 'claude-opus-5-5'], coder: ['p-openai', 'gpt-5.4'], archive: ['p-ollama', 'llama3.3'] };
function mockModelId(providerId, name) {
  const m = mockModels.find((x) => x.provider_id === providerId && x.name === name);
  return m ? m.id : null;
}
for (const bot of mockBots) {
  const [providerId, name] = MOCK_BINDINGS[bot.id];
  bot.provider_id = mockProviders.some((p) => p.id === providerId) ? providerId : null;
  bot.model_id = bot.provider_id ? mockModelId(providerId, name) : null;
  if (bot.provider_id) bot.model = name;
  bot.need_restart = false;
  bot.auto_compact_percent = 80;
}
if (MOCK_PROVIDERS_MODE === 'private') {
  // Архив на провайдере, который ждёт одобрения; Кодер на провайдере с изменившимся адресом.
  Object.assign(mockBots.find((b) => b.id === 'archive'), { provider_id: 'p-lan', model_id: mockModelId('p-lan', 'llama3.3'), model: 'llama3.3' });
  Object.assign(mockBots.find((b) => b.id === 'coder'), { provider_id: 'p-lan-re', model_id: mockModelId('p-lan-re', 'qwen3'), model: 'qwen3' });
}
if (params.get('bots') === 'states' && MOCK_PROVIDERS_MODE !== 'none') {
  const patch = (id, fields) => Object.assign(mockBots.find((b) => b.id === id), fields);
  const reset = { status_label: undefined, status_kind: undefined, summary: undefined };
  patch('archive', { ...reset, status: 'no_model', provider_id: null, model_id: null });
  patch('coder', { ...reset, status: 'error_starting', recreate_url: '/api/bots/coder/recreate' });
  patch('sre', { need_restart: true });
  mockBots.push({
    id: 'spare', name: 'Запасной', role: 'Бот без истории', avatar: 'robot', provider: 'claude', model: 'claude-sonnet-5',
    provider_id: 'p-claude', model_id: mockModelId('p-claude', 'claude-sonnet-5'), executor: 'container', mac_full_control: false,
    status: 'idle', auto_allow: [], budget_daily_tokens: 200000, need_restart: false, auto_compact_percent: 80,
  });
}

const mockThreads = {
  scout: { id: 't-scout', bot_id: 'scout', kind: 'direct', title: 'Поиск вакансий SRE', dry_run: false },
  mac: { id: 't-mac', bot_id: 'mac', kind: 'direct', title: 'Поиск файла на Mac', dry_run: false },
  sre: { id: 't-sre', bot_id: 'sre', kind: 'incident', title: 'Инцидент: 502 webapp', dry_run: false },
  coder: { id: 't-coder', bot_id: 'coder', kind: 'direct', title: 'Обновление зависимостей', dry_run: false },
  archive: { id: 't-archive', bot_id: 'archive', kind: 'routine', title: 'Папка inbox', dry_run: false },
};

const mockEvents = {
  't-mac': [
    { seq: 1, kind: 'system', actor: 'system', payload: { text: 'Начато на iPhone · 09:41' } },
    { seq: 2, kind: 'user_msg', actor: 'owner', client: 'iphone', payload: { text: 'Найди на маке PDF с договором аренды за 2025' } },
    { seq: 3, kind: 'system', actor: 'system', payload: { text: 'Mac спал, разбудил по сети · 11 с' } },
    { seq: 4, kind: 'plan', actor: 'bot:mac', payload: { steps: [
      { id: '1', title: 'Spotlight: найти PDF за 2025', tool: 'mac_find_files', status: 'done' },
      { id: '2', title: 'QuickLook: превью совпадений', tool: 'mac_call', status: 'done' },
      { id: '3', title: 'Отдать список файлов', status: 'done' },
    ] } },
    { seq: 5, kind: 'usage', actor: 'bot:mac', payload: { tokens_in: 900, tokens_out: 220, model: 'Sonnet 5', seconds: 14 } },
    { seq: 6, kind: 'assistant_msg', actor: 'bot:mac', payload: { text: 'Нашёл 3 файла. Самый свежий изменён 12 марта.', final: true } },
    { seq: 7, kind: 'file', actor: 'bot:mac', payload: { file_id: 'f1', name: 'Договор аренды 2025.pdf', size: 1258000, mime: 'application/pdf', origin: 'mac:~/Documents/Квартира/', modified: '12 мар 2025' } },
    { seq: 8, kind: 'file', actor: 'bot:mac', payload: { file_id: 'f2', name: 'Договор аренды 2025 (скан).pdf', size: 4980000, mime: 'application/pdf', origin: 'mac:~/Downloads/', modified: '3 янв 2025' } },
  ],
  't-scout': [
    { seq: 1, kind: 'user_msg', actor: 'owner', client: 'iphone', payload: { text: 'Найди вакансии SRE remote в Европе и подготовь отклики' } },
    { seq: 2, kind: 'plan', actor: 'bot:scout', payload: { steps: [
      { id: '1', title: 'Собрать вакансии SRE remote/EU', status: 'done' },
      { id: '2', title: 'Отобрать 5 лучших', status: 'done' },
      { id: '3', title: 'Подготовить отклик на первую', status: 'running' },
    ] } },
    { seq: 3, kind: 'assistant_msg', actor: 'bot:scout', payload: { text: 'Нашёл 7 вакансий SRE, сохранил 5 лучших. Жду решения по отклику на «Senior SRE, Belgrade/remote».', final: true } },
    { seq: 4, kind: 'approval_req', actor: 'bot:scout', payload: { approval_id: 'ap1', risk: 'send', title: 'Отправить отклик на «Senior SRE, Belgrade/remote»', tool: 'browser.submit_form', expires_at: minutesAgo(-27) } },
  ],
  't-sre': [
    { seq: 1, kind: 'system', actor: 'system', payload: { text: 'Запущено из алерта Alertmanager · 09:12' } },
    { seq: 2, kind: 'plan', actor: 'bot:sre', payload: { steps: [
      { id: '1', title: 'docker ps -a', status: 'done' },
      { id: '2', title: 'docker events --since 1h', status: 'done' },
      { id: '3', title: 'dmesg | grep -i kill', status: 'done' },
      { id: '4', title: 'df -h', status: 'done' },
      { id: '5', title: 'nginx error.log', status: 'done' },
      { id: '6', title: 'Поднять лимит и перезапустить', status: 'waiting_approval' },
    ] } },
    { seq: 3, kind: 'assistant_msg', actor: 'bot:sre', payload: { text: 'Причина почти наверняка в памяти: после деплоя 08:55 контейнер упирается в лимит 1 ГБ, ядро убивает процесс 4 раза.', final: true } },
    { seq: 4, kind: 'usage', actor: 'bot:sre', payload: { tokens_in: 42000, tokens_out: 16000, model: 'Opus 5.5', seconds: 124 } },
    { seq: 5, kind: 'approval_req', actor: 'bot:sre', payload: { approval_id: 'ap2', risk: 'other', title: 'Поднять лимит до 2 ГБ и перезапустить webapp', tool: 'mac_shell', expires_at: minutesAgo(-40) } },
  ],
  't-coder': [
    { seq: 1, kind: 'user_msg', actor: 'owner', client: 'mac', payload: { text: 'Обнови зависимости и прогоняй тесты' } },
    { seq: 2, kind: 'plan', actor: 'bot:coder', payload: { steps: [
      { id: '1', title: 'npm install', status: 'error', error: 'ERESOLVE unable to resolve dependency tree' },
    ] } },
    { seq: 3, kind: 'guard', actor: 'system', payload: { reason: 'repeat_error', detail: '3 раза одна ошибка: npm ERR! ERESOLVE unable to resolve dependency tree' } },
    { seq: 4, kind: 'assistant_msg', actor: 'bot:coder', payload: { text: 'Остановлено на шаге 1. npm install падает с ERESOLVE три раза подряд, дальше не пробую.', final: true } },
  ],
  't-archive': [
    { seq: 1, kind: 'system', actor: 'system', payload: { text: 'Наблюдение за ~/BotHub/inbox' } },
    { seq: 2, kind: 'assistant_msg', actor: 'bot:archive', payload: { text: 'Вчера разложил 4 скана по папкам. Сейчас папка пуста, жду новые файлы.', final: true } },
  ],
};

// ---------------------------------------------------------------------------
// Мок контекста треда (этап 8): заполнение, сжатие, автосжатие. Параметры адреса: &ctx=<проценты> (заполнение всех
// тредов), &ctx=none (данных нет), &ctxwin=none (окно модели неизвестно), &ctxest=1 (значение помечено оценкой),
// &ctxstep=<проценты> (прирост за обычный ход, по умолчанию 2), &ctxreduce=<проценты> (на сколько сжатие уменьшает
// контекст, по умолчанию 75), &ctxfail=1 (сжатие завершается ошибкой). Обычные ходы мока увеличивают tokens, сжатие
// (POST /threads/{id}/compact и автосжатие по порогу бота, не чаще раза в 3 хода) уменьшает и обновляет summary.
// Для e2e: window.__ctxMock.set(threadId, patch, {notify}), .setPercent(threadId, pct, {notify}), .busy(threadId, bool),
// .push(threadId, kind, payload) (событие в ленту без смены состояния) и .bot(botId); set по умолчанию публикует
// событие status, после которого клиент перечитывает тред, как после настоящего хода.
// ---------------------------------------------------------------------------
const MOCK_CTX_WINDOW = params.get('ctxwin') === 'none' ? null : 200000;
const MOCK_CTX_SEED = { 't-scout': 12, 't-mac': 19, 't-sre': 74, 't-coder': 93, 't-archive': 3 };
const mockCtx = {};
let mockTurnSeq = 0;

function mockCtxNumber(name, fallback) {
  const raw = params.get(name);
  const n = raw === null || raw === '' ? NaN : Number(raw);
  return Number.isFinite(n) ? n : fallback;
}
function mockCtxState(threadId) {
  if (!mockCtx[threadId]) {
    const raw = params.get('ctx');
    const pct = raw === 'none' ? undefined : mockCtxNumber('ctx', MOCK_CTX_SEED[threadId]);
    mockCtx[threadId] = {
      tokens: pct === undefined ? null : Math.round((MOCK_CTX_WINDOW || 200000) * pct / 100),
      window: MOCK_CTX_WINDOW,
      estimated: params.get('ctxest') === '1',
      compacted_at: null,
      compactions: 0,
      summary: '',
      auto_compact_disabled: false,
      activeTurn: null, // id активного хода (как queued/running на сервере)
      turnsSince: 3, // обычных ходов после последнего сжатия: автосжатие не чаще раза в 3 хода
    };
  }
  return mockCtx[threadId];
}
function mockContextView(st) {
  const percent = st.tokens != null && st.window ? Math.max(0, Math.min(100, Math.floor(st.tokens * 100 / st.window))) : null;
  return { tokens: st.tokens, window: st.window, percent, estimated: st.estimated, compacted_at: st.compacted_at, compactions: st.compactions };
}
function mockPushEvent(threadId, kind, payload, turnId = null, actor = 'system') {
  const arr = mockEvents[threadId] || (mockEvents[threadId] = []);
  arr.push({ seq: (arr[arr.length - 1]?.seq || 0) + 1, ts: new Date().toISOString(), turn_id: turnId, kind, actor, payload });
}
function mockThreadBot(threadId) {
  return mockBots.find((b) => mockThreads[b.id] && mockThreads[b.id].id === threadId);
}

// Конец обычного хода: tokens растут, затем автосжатие, если заполнение достигло порога бота.
function mockFinishTurn(threadId, turnId) {
  const st = mockCtxState(threadId);
  const size = st.window || 200000;
  st.tokens = Math.min(size, (st.tokens || 0) + Math.round(size * mockCtxNumber('ctxstep', 2) / 100));
  st.estimated = false;
  st.activeTurn = null;
  st.turnsSince += 1;
  mockPushEvent(threadId, 'status', { turn_id: turnId, status: 'done' }, turnId);
  const threshold = mockThreadBot(threadId)?.auto_compact_percent;
  const percent = mockContextView(st).percent;
  if (threshold != null && !st.auto_compact_disabled && st.turnsSince >= 3 && percent !== null && percent >= threshold) {
    setTimeout(() => { if (!st.activeTurn) mockStartCompact(threadId, true); }, 200);
  }
}

function mockStartCompact(threadId, auto) {
  const st = mockCtxState(threadId);
  const turnId = `turn-compact-${++mockTurnSeq}`;
  st.activeTurn = turnId;
  setTimeout(() => mockPushEvent(threadId, 'status', { turn_id: turnId, status: 'running' }, turnId), 300);
  setTimeout(() => mockFinishCompact(threadId, turnId, auto), 1200);
  return { id: turnId, thread_id: threadId, status: 'queued', turn_type: 'compact', created_at: new Date().toISOString() };
}
function mockFinishCompact(threadId, turnId, auto) {
  const st = mockCtxState(threadId);
  if (params.get('ctxfail') === '1') {
    mockPushEvent(threadId, 'compact_failed', { detail: 'mock: сжатие не удалось' }, turnId);
    st.activeTurn = null;
    mockPushEvent(threadId, 'status', { turn_id: turnId, status: 'error' }, turnId);
    return;
  }
  const before = st.tokens;
  const reduce = Math.max(0, Math.min(100, mockCtxNumber('ctxreduce', 75)));
  const after = Math.max(1, Math.round(before * (100 - reduce) / 100));
  st.tokens = after;
  st.estimated = true; // после сжатия размер оценочный, пока бот не пришлёт настоящий расход
  st.compactions += 1;
  st.compacted_at = new Date().toISOString();
  st.turnsSince = 0;
  if (!st.summary || st.summary.startsWith('Краткая сводка диалога')) st.summary = `Краткая сводка диалога (сжатий: ${st.compactions}): владелец поставил задачу, бот собрал результаты и ждёт следующего шага.`;
  const reduction = before ? Math.round((before - after) * 100 / before) : null;
  mockPushEvent(threadId, 'compacted', { tokens_before: before, tokens_after: after, auto, reduction_percent: reduction, summary_chars: st.summary.length }, turnId);
  if (auto && reduction !== null && reduction < 10) {
    st.auto_compact_disabled = true;
    mockPushEvent(threadId, 'auto_compact_disabled', { reduction_percent: reduction }, turnId);
  }
  st.activeTurn = null;
  mockPushEvent(threadId, 'status', { turn_id: turnId, status: 'done' }, turnId);
}
// POST /api/threads/{id}/compact: 409 при активном ходе или когда сжимать нечего (в моке: нет данных о заполнении).
async function mockCompactThread(id) {
  await delay(150);
  if (!Object.values(mockThreads).some((t) => t.id === id)) mockFail(404, 'not_found');
  const st = mockCtxState(id);
  if (st.activeTurn) mockFail(409, 'conflict', 'thread has an active turn');
  if (st.tokens == null) mockFail(409, 'conflict', 'nothing to compact');
  return mockStartCompact(id, false);
}
function mockThreadView(thread) {
  const st = mockCtxState(thread.id);
  return { ...clone(thread), summary: st.summary, auto_compact_disabled: st.auto_compact_disabled, context: mockContextView(st) };
}
if (MOCK) {
  window.__ctxMock = {
    get: (threadId) => ({ ...mockCtxState(threadId) }),
    set: (threadId, patch, { notify = true } = {}) => {
      Object.assign(mockCtxState(threadId), patch);
      if (notify) mockPushEvent(threadId, 'status', { turn_id: 'turn-mock-set', status: 'done' }, 'turn-mock-set');
    },
    setPercent: (threadId, percent, options) => {
      const st = mockCtxState(threadId);
      window.__ctxMock.set(threadId, { tokens: Math.round((st.window || 200000) * percent / 100) }, options);
    },
    // Произвольное событие в ленту треда (compacted, compact_failed, auto_compact_disabled и т. п.): мок состояния не меняет.
    push: (threadId, kind, payload, turnId = null) => mockPushEvent(threadId, kind, payload, turnId),
    bot: (botId) => clone(mockBots.find((b) => b.id === botId) || null),
    busy: (threadId, on) => {
      const st = mockCtxState(threadId);
      st.activeTurn = on ? 'turn-mock-busy' : null;
      mockPushEvent(threadId, 'status', { turn_id: 'turn-mock-busy', status: on ? 'running' : 'done' }, 'turn-mock-busy');
    },
  };
}

const mockApprovals = [
  { id: 'ap1', thread_id: 't-scout', turn_id: 'tu1', bot_id: 'scout', risk: 'send', title: 'Отправить отклик на «Senior SRE, Belgrade/remote»', tool: 'browser.submit_form', args: { url: 'jobs.example.eu/812', data: 'Резюме EN, email' }, args_hash: '4f2a9c1', status: 'pending', expires_at: minutesAgo(-27) },
  { id: 'ap2', thread_id: 't-sre', turn_id: 'tu2', bot_id: 'sre', risk: 'other', title: 'Поднять лимит до 2 ГБ и перезапустить webapp', tool: 'mac_shell', args: { cmd: 'docker update --memory=2g webapp && docker restart webapp' }, args_hash: '9b1e73d', status: 'pending', expires_at: minutesAgo(-40) },
];

let mockMemorySeq = 10;
// Предел ядра для текста записи памяти (16 КиБ в UTF-8), чтобы мок отвечал 400 так же, как PATCH и POST /api/memory.
const memoryTextTooLong = (text) => new TextEncoder().encode(text).length > 16 * 1024;
const mockMemoryProposed = [
  { id: 'm-p1', bot_id: 'scout', text: 'Резюме EN для откликов лежит в ~/Documents/CV/Resume_EN.pdf', source: 'bot:scout', status: 'proposed', version: 1, expires_at: null, created_at: minutesAgo(70) },
];
const mockMemoryActive = [
  { id: 'm1', bot_id: 'sre', text: 'server: диск 280 ГБ, Ubuntu, Docker', source: 'bot:sre', status: 'active', version: 1, expires_at: null, created_at: minutesAgo(60 * 24 * 11) },
  { id: 'm2', bot_id: 'mac', text: 'Договоры по квартире: ~/Documents/Квартира', source: 'bot:mac', status: 'active', version: 1, expires_at: null, created_at: minutesAgo(60 * 3) },
  { id: 'm3', bot_id: null, text: 'Релокация: сначала Сербия, потом Япония', source: 'owner', status: 'active', version: 1, expires_at: null, created_at: minutesAgo(60 * 24 * 13) },
  { id: 'm4', bot_id: 'sre', text: 'Сертификаты: certbot --nginx, без wildcard', source: 'bot:sre', status: 'active', version: 1, expires_at: minutesAhead(60 * 24 * 30), created_at: minutesAgo(60 * 24 * 3) },
];

const mockSchedules = [
  { id: 's1', bot_id: 'sre', name: 'Проверка серверов', kind: 'cron', cron: '0 3 * * *', prompt: 'Проверить здоровье серверов', enabled: true, next_run_at: minutesAgo(-17 * 60), last_run: { ok: true, at: 'сегодня 03:00', seconds: 120 },
    catch_up: false, skipped_count: 6, last_skipped_at: minutesAgo(30), last_skip_reason: 'executor_unavailable', paused_by_unavailable: true },
  { id: 's2', bot_id: 'archive', name: 'Разбор почты', kind: 'cron', cron: '30 8 * * 1-5', prompt: 'Разобрать почту', enabled: false, paused_since: '20 сен', catch_up: false, skipped_count: 0, paused_by_unavailable: false },
  { id: 's3', bot_id: 'sre', name: 'Алерт → диагностика', kind: 'hook', prompt: 'Диагностировать алерт', enabled: true, last_run: { at: 'сегодня 09:12', detail: '502 на webapp' },
    catch_up: false, skipped_count: 2, last_skipped_at: minutesAgo(45), last_skip_reason: 'bot_paused', paused_by_unavailable: false },
  { id: 's4', bot_id: 'archive', name: 'Папка ~/BotHub/inbox', kind: 'mac_folder', prompt: 'Разложить новые файлы', enabled: true, last_run: { at: 'вчера', detail: '4 скана разложены по папкам' } },
];

// ---------------------------------------------------------------------------
// Мок ленты активности и паузы (docs/contracts.md §16). События такие же, как отдаёт ядро: title {code, params},
// новые сверху, курсор непрозрачный. Параметры адреса: &activity=none (пусто), &activity=fail (первая загрузка
// падает), &activity=slow (долгая загрузка), &activity=more-fail (догрузка «Показать ещё» падает один раз),
// &activity=xss (тексты событий с разметкой), &pause=fail (пауза бота отвечает 500).
// ---------------------------------------------------------------------------
const MOCK_ACTIVITY_MODE = params.get('activity') || '';
const MOCK_PAUSE_FAIL = params.get('pause') === 'fail';
let mockActivityFailed = false;
let mockActivityMoreFailed = false;
let mockActivitySeq = 0;
const mockActivity = [];
// Время события. «Минут назад» до 3 ч это сегодня: если мок открыт вскоре после полуночи, такие события сжимаются
// в границы текущих суток, а не уезжают во вчера. От 3 до 30 ч это вчера (растянуто на весь вчерашний день), дальше
// идут более ранние дни. Так группы «Сегодня» и «Вчера» есть в любой час, а порядок событий не зависит от часа запуска.
function mockActivityAt(minutes) {
  const TODAY = 180;
  const YESTERDAY = 1800;
  const dayStart = (back) => { const d = new Date(now); d.setHours(0, 0, 0, 0); d.setDate(d.getDate() - back); return d.getTime(); };
  if (minutes <= TODAY) {
    const room = Math.max(now - dayStart(0) - 1000, 1);
    return new Date(now - minutes * 60000 * Math.min(1, room / (TODAY * 60000))).toISOString();
  }
  const yesterday = dayStart(1);
  if (minutes <= YESTERDAY) {
    const share = (YESTERDAY - minutes) / (YESTERDAY - TODAY);
    return new Date(yesterday + 1000 + share * (dayStart(0) - yesterday - 2000)).toISOString();
  }
  return new Date(yesterday - 1000 - (minutes - YESTERDAY) * 60000).toISOString();
}
function mockLog(minutes, kind, code, bot_id, extra = {}) {
  mockActivity.push({
    id: `mock:${String(++mockActivitySeq).padStart(4, '0')}`, at: mockActivityAt(minutes), bot_id, thread_id: extra.thread_id ?? null,
    ...(extra.turn_id ? { turn_id: extra.turn_id } : {}), kind, title: { code, params: extra.params || {} },
    ...(extra.detail ? { detail: extra.detail } : {}), ...(extra.risk ? { risk: extra.risk } : {}), ...(extra.status ? { status: extra.status } : {}),
  });
}
if (MOCK_ACTIVITY_MODE !== 'none') {
  mockLog(5, 'turn', 'turn_started', 'scout', { thread_id: 't-scout', turn_id: 'tu1', status: 'running', params: { client: 'iphone' } });
  mockLog(6, 'browser', 'browser_step', 'scout', { thread_id: 't-scout', turn_id: 'tu1', status: 'ok', params: { action: 'navigate', url: 'https://jobs.example.eu/812' } });
  mockLog(5.6, 'browser', 'browser_step', 'scout', { thread_id: 't-scout', turn_id: 'tu1', status: 'ok', params: { action: 'click', target: 'Откликнуться' } });
  mockLog(5.2, 'browser', 'browser_step', 'scout', { thread_id: 't-scout', turn_id: 'tu1', status: 'ok', params: { action: 'fill', target: '[redacted]' } });
  mockLog(4, 'approval', 'approval_requested', 'scout', { thread_id: 't-scout', turn_id: 'tu1', risk: 'send', status: 'pending', params: { tool: 'browser.submit_form' }, detail: 'Отправить отклик на «Senior SRE, Belgrade/remote»' });
  mockLog(20, 'turn', 'turn_started', 'sre', { thread_id: 't-sre', turn_id: 'tu2', status: 'running', params: { client: 'hook' } });
  mockLog(21, 'schedule', 'hook_run', 'sre', { thread_id: 't-sre', turn_id: 'tu2', status: 'running', params: { name: 'Алерт → диагностика', schedule_id: 's3' } });
  mockLog(18, 'approval', 'approval_requested', 'sre', { thread_id: 't-sre', turn_id: 'tu2', risk: 'other', status: 'pending', params: { tool: 'mac_shell' }, detail: 'Поднять лимит до 2 ГБ и перезапустить webapp' });
  mockLog(30, 'schedule', 'schedule_skipped', 'sre', { params: { reason: 'executor_unavailable', count: 5, paused: true, schedule_id: 's1', name: 'Проверка серверов' } });
  mockLog(75, 'takeover', 'takeover_started', 'scout', { thread_id: 't-scout', params: { from: 'bot', to: 'human' } });
  mockLog(66, 'takeover', 'takeover_returned', 'scout', { thread_id: 't-scout', params: { from: 'human', to: 'returning' } });
  mockLog(120, 'turn', 'turn_started', 'mac', { thread_id: 't-mac', turn_id: 'tu3', status: 'done', params: { client: 'iphone' } });
  mockLog(118, 'turn', 'turn_done', 'mac', { thread_id: 't-mac', turn_id: 'tu3', status: 'done', params: { client: 'iphone' } });
  mockLog(125, 'memory', 'memory_proposed', 'mac', { status: 'proposed', detail: 'Договоры по квартире: ~/Documents/Квартира', params: { memory_id: 'm5' } });
  mockLog(60 * 5, 'approval', 'approval_requested', 'sre', { thread_id: 't-sre', risk: 'pay', status: 'approved', params: { tool: 'browser.click' }, detail: 'Оплатить подписку на хостинг' });
  mockLog(60 * 5 - 2, 'approval', 'approval_approved', 'sre', { thread_id: 't-sre', risk: 'pay', status: 'approved', params: { tool: 'browser.click' }, detail: 'Оплатить подписку на хостинг' });
  mockLog(60 * 6, 'turn', 'turn_error', 'coder', { thread_id: 't-coder', turn_id: 'tu4', status: 'error', params: { client: 'api' } });
  mockLog(60 * 20, 'procedure', 'procedure_started', 'scout', { thread_id: 't-scout', status: 'done', params: { name: 'Вход в кабинет работодателя', procedure_id: 'pr1', run_id: 'run1' } });
  mockLog(60 * 20 - 1, 'procedure', 'procedure_finished', 'scout', { thread_id: 't-scout', status: 'done', params: { name: 'Вход в кабинет работодателя', procedure_id: 'pr1', run_id: 'run1' } });
  mockLog(60 * 26, 'pause', 'bot_paused', 'archive', { params: { reason: 'Отпуск' } });
  mockLog(60 * 28, 'pause', 'bot_resumed', 'archive');
  mockLog(60 * 29, 'schedule', 'schedule_resumed', 'archive', { params: { schedule_id: 's2', name: 'Разбор почты' } });
  if (MOCK_ACTIVITY_MODE === 'xss') {
    // Тексты от бота и пользователя приходят как данные: в разметку попадают только экранированными.
    const bad = '<img src=x onerror="window.__activityXss=1">';
    mockLog(1, 'memory', 'memory_proposed', 'mac', { status: 'proposed', detail: bad, params: { memory_id: 'mx' } });
    mockLog(1.5, 'approval', 'approval_requested', 'scout', { thread_id: 't-scout', risk: 'other', status: 'pending', params: { tool: bad }, detail: bad });
    mockLog(2, 'browser', 'browser_step', 'scout', { thread_id: 't-scout', status: 'ok', params: { action: 'navigate', url: bad, target: bad } });
    mockLog(2.5, 'schedule', 'schedule_run', 'sre', { thread_id: 't-sre', params: { name: bad } });
  }
  const filler = ['scout', 'mac', 'sre', 'coder', 'archive'];
  for (let i = 0; i < 70; i += 1) {
    const bot = filler[i % filler.length];
    const done = i % 7 !== 3;
    mockLog(60 * 31 + i * 47, 'turn', done ? 'turn_done' : 'turn_stopped', bot, { thread_id: `t-${bot}`, turn_id: `tf${i}`, status: done ? 'done' : 'stopped', params: { client: 'schedule' } });
  }
}
mockActivity.sort((a, b) => (a.at === b.at ? (a.id < b.id ? 1 : -1) : (a.at < b.at ? 1 : -1)));
const mockCursor = (item) => btoa(JSON.stringify([item.at, item.id]));
function mockRunningTurn(bot) { return ['running', 'waiting', 'waiting_approval', 'waiting_mac'].includes(bot.status) ? `tu-${bot.id}` : null; }
function mockPauseView(bot) {
  return { bot_id: bot.id, paused: !!bot.paused, paused_at: bot.paused_at || null, paused_reason: bot.paused_reason || null, running_turn: mockRunningTurn(bot) };
}
function mockSetPaused(bot, paused, reason) {
  if (!!bot.paused === paused) return;
  bot.paused = paused;
  bot.paused_at = paused ? new Date().toISOString() : null;
  bot.paused_reason = paused ? (reason || null) : null;
  const item = { id: `mock:p${String(++mockActivitySeq).padStart(4, '0')}`, at: new Date().toISOString(), bot_id: bot.id, thread_id: null, kind: 'pause', title: { code: paused ? 'bot_paused' : 'bot_resumed', params: reason && paused ? { reason } : {} } };
  mockActivity.unshift(item);
}

const MOCK_USAGE_MODE = params.get('usage') || '';
// usage=xss: имя бота и модели с HTML, экран обязан показать его текстом.
const MOCK_USAGE_XSS = '<img src=x onerror="window.__usageXss=1">';
if (MOCK_USAGE_MODE === 'xss') mockBots.find((b) => b.id === 'scout').name = MOCK_USAGE_XSS;
let mockUsageListFailed = false;

function buildMockDaily(days) {
  const list = [];
  const baseTokens = [142000, 98000, 165000, 120000, 85000, 190000, 155000];
  for (let i = days - 1; i >= 0; i--) {
    const d = new Date(now - i * 86400000);
    const dateStr = d.toISOString().slice(0, 10);
    const dayTokens = i === 0 ? 155000 : baseTokens[i % baseTokens.length];
    list.push({ date: dateStr, total_tokens: dayTokens });
  }
  return list;
}

function buildMockUsage(days = 7, empty = false) {
  if (empty) {
    return {
      days,
      total_tokens: 0,
      bots: [],
      models: [],
      daily: [],
      providers: [],
      guard: null,
    };
  }
  const daily = buildMockDaily(days);
  const total_tokens = daily.reduce((sum, d) => sum + d.total_tokens, 0);
  const mult = Math.max(1, Math.min(days, 30));
  return {
    days,
    total_tokens,
    providers: [
      { provider: 'claude', pct_week: 42, reset_at: 'пн 03:00' },
      { provider: 'codex', pct_week: 18, reset_at: 'пн 03:00' },
      { provider: 'gemini', pct_week: 7, reset_at: 'пн 03:00' },
    ],
    bots: [
      { bot_id: 'scout', tokens_today: 128000, tokens_period: 128000 * mult, budget: 200000, last_activity: minutesAgo(12) },
      { bot_id: 'mac', tokens_today: 24000, tokens_period: 24000 * mult, budget: 200000, last_activity: minutesAgo(45) },
      { bot_id: 'sre', tokens_today: 70000, tokens_period: 70000 * mult, budget: 200000, last_activity: minutesAgo(120) },
      { bot_id: 'coder', tokens_today: 200000, tokens_period: 200000 * mult, budget: 200000, last_activity: minutesAgo(30) },
      { bot_id: 'archive', tokens_today: 10000, tokens_period: 10000 * mult, budget: 200000, last_activity: minutesAgo(360) },
    ],
    models: [
      { provider: 'claude', model: 'claude-sonnet-5', tokens_in: 180000 * mult, tokens_out: 45000 * mult, tokens_cache_read: 120000 * mult, tokens_cache_write: 30000 * mult, total_tokens: 375000 * mult, turns: 42 * mult },
      { provider: 'claude', model: 'claude-opus-5-5', tokens_in: 90000 * mult, tokens_out: 25000 * mult, tokens_cache_read: 60000 * mult, tokens_cache_write: 15000 * mult, total_tokens: 190000 * mult, turns: 18 * mult },
      { provider: 'codex', model: MOCK_USAGE_MODE === 'xss' ? MOCK_USAGE_XSS : 'gpt-5.4', tokens_in: 110000 * mult, tokens_out: 30000 * mult, tokens_cache_read: 0, tokens_cache_write: 0, total_tokens: 140000 * mult, turns: 25 * mult },
      { provider: 'gemini', model: 'gemini-3.1-pro-preview', tokens_in: 30000 * mult, tokens_out: 10000 * mult, tokens_cache_read: 0, tokens_cache_write: 0, total_tokens: 40000 * mult, turns: 12 * mult },
    ],
    daily,
    guard: { bot_id: 'coder', thread_id: 't-coder', reason: 'repeat_error', detail: 'npm ERR! ERESOLVE unable to resolve dependency tree' },
  };
}

const mockUsage = buildMockUsage(7);

const mockMac = { state: 'online', last_seen: new Date().toISOString(), info: { host: 'MacBook Air', control: 'full', net: 'Tailscale', latency_ms: 21 } };

function clone(v) { return JSON.parse(JSON.stringify(v)); }
function delay(ms = 120) { return new Promise((r) => setTimeout(r, ms)); }

// ---------------------------------------------------------------------------
// Мок-черновик конструктора ботов (docs/contracts.md §9, POST /api/bots/draft)
// ---------------------------------------------------------------------------
function hashStr(s) {
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0;
  return Math.abs(h);
}
const DRAFT_NAMES = ['Наблюдатель', 'Секретарь', 'Дозорный', 'Сборщик', 'Хелпер', 'Штурман'];
const DRAFT_MODELS = ['claude-haiku-4-5-20251001', 'claude-sonnet-5', 'claude-opus-5-5'];
const DRAFT_MODEL_LABEL = { [DRAFT_MODELS[0]]: 'Haiku', [DRAFT_MODELS[1]]: 'Sonnet', [DRAFT_MODELS[2]]: 'Opus' };

function buildMockDraft(description) {
  const lower = description.toLowerCase();
  const h = hashStr(description);
  const id = `draft-${h.toString(36).slice(0, 8)}`;
  const name = DRAFT_NAMES[h % DRAFT_NAMES.length];
  const avatar = AVATAR_KINDS[h % AVATAR_KINDS.length];
  const isMac = /\bмак(е|а|у)?\b|файл(ы)? на маке|скриншот|приложени/.test(lower);
  const complex = /анализ|сравн|исследу|разбер/.test(lower);
  const routine = !complex && /напомин|провер|смотр|следи/.test(lower);
  const model = complex ? DRAFT_MODELS[2] : routine ? DRAFT_MODELS[0] : DRAFT_MODELS[1];
  const executor = isMac ? 'mac' : 'container';
  const macFullControl = isMac && /открой|клик|запусти|нажми/.test(lower);
  const autoAllow = executor === 'mac'
    ? [{ tool: 'mcp__bothub__mac_find_files' }, { tool: 'mcp__bothub__mac_read_file' }]
    : [{ tool: 'mcp__bothub__shell' }];
  const timeMatch = lower.match(/\bв (\d{1,2})(?::(\d{2}))?\b/);
  const hasSchedule = timeMatch || /кажд(ое|ый)|по будням|ежедневно|раз в день/.test(lower);
  let schedule = null;
  if (hasSchedule) {
    const hour = timeMatch ? Number(timeMatch[1]) : (/вечер/.test(lower) ? 19 : 9);
    const minute = timeMatch && timeMatch[2] ? Number(timeMatch[2]) : 0;
    const weekdays = /будням|будни/.test(lower);
    schedule = { name: `Задача: ${name}`, cron: `${minute} ${hour} * * ${weekdays ? '1-5' : '*'}`, timezone: 'Europe/Moscow', prompt: description };
  }
  const role = description.length > 70 ? `${description.slice(0, 67).trim()}…` : description;
  const modelLabel = DRAFT_MODEL_LABEL[model];
  const rationale = [
    `Модель ${modelLabel}: ${complex ? 'в задаче есть анализ и сравнение' : routine ? 'задача рутинная, без сложных решений' : 'обычная задача, сложного анализа не увидел'}.`,
    executor === 'mac' ? 'Работает на Mac: в описании речь про файлы или экран.' : 'Работает на сервере: доступ к маку не нужен.',
    schedule ? 'Добавил расписание по словам о времени в описании.' : 'Расписания в описании не нашёл, бот будет ждать сообщений.',
  ].join(' ');
  const instructions = `Задача от владельца: «${description}».\nРаботай самостоятельно, о неочевидных решениях сообщай коротко.\n\nОбщее для всех ботов: делегируй через mac_delegate, если Mac в сети, иначе делай сам; необратимое действие всегда через подтверждение; результат подтверждай фактом, а не предположением; пиши по-русски.`;
  return { id, name, role, avatar, provider: 'claude', model, executor, mac_full_control: macFullControl, auto_allow: autoAllow, instructions, schedule, rationale };
}

// ---------------------------------------------------------------------------
// Мок входа и пользователей. Параметры адреса: &role=member (войти участником), &auth=none (старт без сессии),
// &setup=1 (пользователей ещё нет, нужна первичная настройка). Состояние в sessionStorage переживает перезагрузку.
// Пароли-триггеры: wrong-password даёт 401, too-many-attempts даёт 429, server-down даёт 500.
// ---------------------------------------------------------------------------

const MOCK_SESSION_KEY = 'bothub_mock_session';
const MOCK_SETUP_KEY = 'bothub_mock_setup_done';
const MOCK_SETUP_CODE = 'setup-code';
const mockStore = {
  get(key) { try { return sessionStorage.getItem(key); } catch { return null; } },
  set(key, value) { try { sessionStorage.setItem(key, value); } catch { /* приватный режим */ } },
};

const mockUsers = [
  { id: 'u-admin', email: 'admin@example.org', role: 'admin', status: 'active', created_at: minutesAgo(60 * 24 * 40) },
  { id: 'u-alice', email: 'alice@example.org', role: 'member', status: 'active', created_at: minutesAgo(60 * 24 * 12) },
  { id: 'u-bob', email: 'bob@example.org', role: 'member', status: 'disabled', created_at: minutesAgo(60 * 24 * 25) },
];
// Пользователи, созданные в этой вкладке (первичная настройка, принятие инвайта), переживают перезагрузку страницы.
const MOCK_USERS_KEY = 'bothub_mock_users';
try {
  const saved = JSON.parse(mockStore.get(MOCK_USERS_KEY) || 'null');
  if (Array.isArray(saved) && saved.length) { mockUsers.length = 0; mockUsers.push(...saved); }
} catch { /* повреждённое значение: остаются пользователи по умолчанию */ }
const mockSaveUsers = () => mockStore.set(MOCK_USERS_KEY, JSON.stringify(mockUsers));

const mockInvites = [
  { token_hash: 'mock-invite-1', created_by: 'u-admin', role: 'member', expires_at: minutesAhead(46 * 60), used_by: null, used_at: null },
  { token_hash: 'mock-invite-2', created_by: 'u-admin', role: 'member', expires_at: minutesAgo(60 * 30), used_by: null, used_at: null },
  { token_hash: 'mock-invite-3', created_by: 'u-admin', role: 'member', expires_at: minutesAhead(60 * 24 * 3), used_by: 'u-bob', used_at: minutesAgo(60 * 24 * 2) },
];
const mockSessions = [
  { id_hash: 'mock-session-1', created_at: minutesAgo(60 * 3), expires_at: minutesAhead(60 * 24 * 30), revoked_at: null, user_agent: 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1' },
  { id_hash: 'mock-session-2', created_at: minutesAgo(60 * 24 * 6), expires_at: minutesAhead(60 * 24 * 24), revoked_at: null, user_agent: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36' },
  { id_hash: 'mock-session-0', created_at: minutesAgo(60 * 24 * 9), expires_at: minutesAhead(60 * 24 * 21), revoked_at: minutesAgo(60 * 24 * 2), user_agent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36 Edg/129.0' },
];
// Токены инвайтов для мока: valid-token работает, race-* проходят проверку, но отклоняются при принятии (гонка), остальные недействительны.
const MOCK_VALID_TOKENS = new Set(['valid-token', 'race-expired-token', 'race-used-token']);
const MOCK_CURRENT_SESSION = 'mock-session-1';

let mockSetupDone = params.get('setup') === '1' ? mockStore.get(MOCK_SETUP_KEY) === '1' : true;
const mockDefaultRole = params.get('role') === 'member' ? 'member' : 'admin';
let mockUserId = (() => {
  const saved = mockStore.get(MOCK_SESSION_KEY);
  if (saved !== null) return saved;
  if (params.get('auth') === 'none' || params.get('setup') === '1') return '';
  return mockDefaultRole === 'member' ? 'u-alice' : 'u-admin';
})();
function mockCurrentUser() { return mockUsers.find((u) => u.id === mockUserId && u.status === 'active') || null; }
function mockSetSession(id) { mockUserId = id; mockStore.set(MOCK_SESSION_KEY, id); }
function mockFail(status, error, detail = error) { throw new ApiError(status, { error, detail }); }
function mockRequireSession() { if (!mockCurrentUser()) { notifyUnauthorized(); mockFail(401, 'unauthorized'); } }
function mockRequireAdmin() { mockRequireSession(); if (mockCurrentUser().role !== 'admin') mockFail(403, 'forbidden'); }

async function mockAuthMe() {
  await delay(60);
  const user = mockCurrentUser();
  if (!user) mockFail(401, 'unauthorized');
  return { ...clone(user), csrf_token: 'mock-csrf', legacy_auth: false };
}
async function mockLogin(email, password) {
  await delay(250);
  const key = String(email || '').trim().toLowerCase();
  if (password === 'too-many-attempts') mockFail(429, 'rate_limited');
  if (password === 'server-down') mockFail(500, 'invalid', 'internal');
  const known = mockUsers.find((u) => u.email === key);
  if (password === 'wrong-password' || !password || (known && known.status !== 'active')) mockFail(401, 'unauthorized', 'invalid_credentials');
  const user = known || mockUsers.find((u) => u.role === mockDefaultRole && u.status === 'active');
  mockSetSession(user.id);
  return { id: user.id, email: user.email, role: user.role, csrf_token: 'mock-csrf' };
}
async function mockLogout() {
  await delay(80);
  mockSetSession('');
  return { ok: true };
}
async function mockChangePassword(oldPassword, newPassword) {
  await delay(200);
  mockRequireSession();
  if (oldPassword === 'too-many-attempts') mockFail(429, 'rate_limited');
  if (oldPassword === 'server-down') mockFail(500, 'invalid', 'internal');
  if (oldPassword === 'wrong-password') mockFail(401, 'unauthorized');
  if (String(newPassword || '').length < 10) mockFail(400, 'invalid');
  mockSetSession('');
  return { ok: true };
}
async function mockSetup(email, password, code) {
  await delay(250);
  if (mockSetupDone) mockFail(409, 'conflict');
  if (code !== MOCK_SETUP_CODE) mockFail(401, 'unauthorized');
  if (!String(email).includes('@') || String(password || '').length < 10) mockFail(400, 'invalid');
  const user = { id: 'u-first', email: String(email).trim().toLowerCase(), role: 'admin', status: 'active', created_at: new Date().toISOString() };
  mockUsers.length = 0;
  mockUsers.push(user);
  mockSaveUsers();
  mockStore.set(MOCK_SETUP_KEY, '1');
  mockSetupDone = true;
  return clone(user);
}
async function mockCreateInvite(role, days) {
  await delay(200);
  mockRequireAdmin();
  if (!['admin', 'member'].includes(role)) mockFail(400, 'invalid');
  const token = `mock-${Math.random().toString(36).slice(2, 12)}`;
  const row = { token_hash: `mock-invite-${mockInvites.length + 1}-${token.slice(5, 9)}`, created_by: mockUserId, role, expires_at: minutesAhead(days * 24 * 60), used_by: null, used_at: null };
  mockInvites.unshift(row);
  MOCK_VALID_TOKENS.add(token);
  return { ...clone(row), token };
}
async function mockDeleteInvite(id) {
  await delay(150);
  mockRequireAdmin();
  const i = mockInvites.findIndex((x) => x.token_hash === id && !x.used_at);
  if (i < 0) mockFail(404, 'not_found');
  mockInvites.splice(i, 1);
  return { ok: true };
}
async function mockAcceptInvite(token, email, password) {
  await delay(300);
  if (token === 'race-expired-token') mockFail(410, 'invite_expired');
  if (token === 'race-used-token') mockFail(410, 'invite_used');
  if (!MOCK_VALID_TOKENS.has(token)) mockFail(410, 'invite_invalid');
  const key = String(email || '').trim().toLowerCase();
  if (!key.includes('@') || String(password || '').length < 10) mockFail(400, 'invalid');
  if (mockUsers.some((u) => u.email === key)) mockFail(409, 'conflict');
  const user = { id: `u-${key.split('@')[0]}`, email: key, role: 'member', status: 'active', created_at: new Date().toISOString() };
  mockUsers.push(user);
  mockSaveUsers();
  MOCK_VALID_TOKENS.delete(token);
  return clone(user);
}
async function mockPatchUser(id, body) {
  await delay(150);
  mockRequireAdmin();
  const user = mockUsers.find((u) => u.id === id);
  if (!user) mockFail(404, 'not_found');
  const role = body.role ?? user.role;
  const status = body.disabled === undefined ? user.status : (body.disabled ? 'disabled' : 'active');
  const activeAdmins = mockUsers.filter((u) => u.role === 'admin' && u.status === 'active').length;
  if (user.role === 'admin' && user.status === 'active' && (role !== 'admin' || status !== 'active') && activeAdmins <= 1) mockFail(409, 'conflict', 'last_admin');
  user.role = role;
  user.status = status;
  return clone(user);
}
async function mockDeleteSession(id) {
  await delay(150);
  mockRequireSession();
  const row = mockSessions.find((x) => x.id_hash === id && !x.revoked_at);
  if (!row) mockFail(404, 'not_found');
  row.revoked_at = new Date().toISOString();
  if (id === MOCK_CURRENT_SESSION) mockSetSession('');
  return { ok: true };
}

// ---------------------------------------------------------------------------
// Мок реестра: проверка, создание, правки, вход по подписке
// ---------------------------------------------------------------------------
let mockProviderSeq = 0;
let mockListFailed = false;
// Что отдаёт API: без служебных полей мока. allow_private_ips показываем только админу (участник их не видит, §11).
const mockPublicProvider = (p) => {
  const { _keyBad, _down, _notApi, _loggedIn, _reapproval, allow_private_ips: ips, ...rest } = p;
  const out = clone(rest);
  if (ips && mockCurrentUser()?.role === 'admin') out.allow_private_ips = clone(ips);
  return out;
};
const mockCall = (name, id, body) => {
  (window.__providerCalls ||= []).push({ name, id, body: body === undefined ? null : clone(body) });
};
const mockProviderById = (id) => {
  const p = mockProviders.find((x) => x.id === id);
  if (!p) mockFail(404, 'not_found');
  return p;
};
async function mockListGate() {
  await delay(MOCK_PROVIDERS_MODE === 'slow' ? 900 : 80);
  if (MOCK_PROVIDERS_MODE === 'fail' && !mockListFailed) {
    mockListFailed = true;
    mockFail(500, 'invalid', 'internal');
  }
}

// Список моделей провайдера после успешной проверки: новые модели включаются, прежние ручные отключения сохраняются.
function mockSyncModels(provider) {
  const key = provider.kind === 'cli_subscription' ? `cli_${provider.cli}` : provider.kind;
  const names = MOCK_VENDOR_MODELS[key] || [];
  for (const name of names) {
    const known = mockModels.find((m) => m.provider_id === provider.id && m.name === name);
    if (!known) mockModels.push(mockModel(provider.id, name));
    else known.enabled = !known.manually_disabled;
  }
  for (const m of mockModels) if (m.provider_id === provider.id && !names.includes(m.name)) m.enabled = false;
}
// Результат проверки по тем же правилам, что у ядра: last_error содержит текст исключения.
function mockRunCheck(p) {
  p.last_check_at = new Date().toISOString();
  let error = null;
  if (p.kind === 'cli_subscription') { if (!p._loggedIn) error = 'subscription not authenticated'; }
  else if (p._keyBad) error = 'provider check failed';
  else if (p._down) error = 'ConnectError';
  else if (p._notApi) error = 'Expecting value: line 1 column 1 (char 0)';
  p.status = error ? 'error' : 'ok';
  p.last_error = error;
  if (!error) mockSyncModels(p);
  return p;
}
// Адрес по правилам §11: forbidden (всегда запрещён), private (нужно одобрение админа), public.
function mockAddressKind(url) {
  const host = String(url || '').replace(/^[a-z]+:\/\//i, '').split(/[/:?#]/)[0].toLowerCase();
  if (/@/.test(String(url)) || !/^https?:\/\//i.test(String(url))) return 'forbidden';
  if (host === 'localhost' || /^127\./.test(host) || /^169\.254\./.test(host) || host === '0.0.0.0') return 'forbidden';
  if (/^192\.168\./.test(host) || /^10\./.test(host) || /^172\.(1[6-9]|2\d|3[01])\./.test(host) || /\.(lan|local)$/.test(host)) return 'private';
  return 'public';
}
const mockSecretTail = (secret) => (secret.length >= 12 ? secret.slice(-4) : null);
// Пробный запрос с ключом-кандидатом: отказ как у ядра, ничего не записывается.
function mockProbe(secret, url) {
  if (/limit/i.test(secret)) mockFail(429, 'rate_limited', 'too many provider probes');
  if (/bad/i.test(secret)) mockFail(422, 'key_rejected', 'provider returned 401');
  if (/offline/i.test(secret) || /down/i.test(url || '')) mockFail(422, 'unreachable', 'connection failed');
  if (/weird/i.test(secret) || /notapi/i.test(url || '')) mockFail(422, 'incompatible', 'response is not a model list');
}
const MOCK_REQUESTS_MODE = params.get('requests') || '';
const mockRequests = MOCK_REQUESTS_MODE ? [
  { id: 'r-lan', name: 'Llama в офисе', base_url: 'https://llm.office.lan/v1', email: 'alice@example.org', created_at: minutesAgo(35), allow_private: false, approved_ips: [], resolved_ips: ['192.168.1.20'], reapproval: false },
  { id: 'r-nas', name: 'NAS с моделями', base_url: 'https://nas.lan/v1', email: 'alice@example.org', created_at: minutesAgo(60 * 30), allow_private: true, approved_ips: ['10.0.0.5'], resolved_ips: ['10.0.0.9', '10.0.0.10'], reapproval: true },
  { id: 'r-gone', name: 'Сервер без DNS', base_url: 'https://gone.lan/v1', email: 'bob@example.org', created_at: minutesAgo(60 * 5), allow_private: false, approved_ips: [], resolved_ips: [], reapproval: false, resolve_error: 'name does not resolve' },
] : [];
let mockStaleDone = false;
let mockRequestsFailed = false;

async function mockCreateProvider(body) {
  await delay(params.get('probe') === 'slow' ? 2500 : 600);
  mockRequireSession();
  mockCall('createProvider', null, body);
  const name = typeof body.name === 'string' ? body.name.trim() : '';
  const kinds = ['anthropic_api', 'openai_api', 'google_api', 'openai_compatible', 'cli_subscription'];
  if (!kinds.includes(body.kind) || !name) mockFail(400, 'invalid');
  if (body.kind === 'cli_subscription') {
    if (!['claude', 'codex', 'agy'].includes(body.cli) || body.secret || body.base_url) mockFail(400, 'invalid');
  } else if (body.cli || typeof body.secret !== 'string' || !body.secret) mockFail(400, 'invalid');
  if (body.kind === 'openai_compatible' && !body.base_url) mockFail(400, 'invalid');
  if (body.force && !body.secret) mockFail(400, 'invalid', 'force requires secret');
  const admin = mockCurrentUser().role === 'admin';
  if ('allow_private' in body && !admin) mockFail(403, 'forbidden', 'allow_private is admin only');
  if (mockProviders.some((p) => p.name === name)) mockFail(409, 'conflict');
  const url = body.base_url || null;
  const address = url ? mockAddressKind(url) : 'public';
  if (address === 'forbidden') mockFail(422, 'invalid_base_url', 'address is not allowed');
  const p = {
    id: `p-new-${++mockProviderSeq}`, kind: body.kind, cli: body.cli || null, name, base_url: url, status: 'new', last_check_at: null,
    last_error: null, has_secret: body.kind !== 'cli_subscription', secret_tail: body.secret ? mockSecretTail(body.secret) : null,
    allow_private: false, created_at: new Date().toISOString(), _loggedIn: false,
  };
  if (body.kind !== 'cli_subscription') {
    if (address === 'private' && !(admin && body.allow_private === true)) {
      // Участнику (и админу без allow_private) пробный запрос не уходит: заявка ждёт администратора.
      p.status = 'pending_admin';
      mockProviders.push(p);
      mockRequests.push({ id: p.id, name, base_url: url, email: mockCurrentUser().email, created_at: p.created_at, allow_private: false, approved_ips: [], resolved_ips: ['192.168.1.20'], reapproval: false });
      return mockPublicProvider(p);
    }
    if (address === 'private') { p.allow_private = true; p.allow_private_ips = ['192.168.1.20']; }
    if (body.force === true) p.status = 'unchecked';
    else {
      mockProbe(body.secret, url);
      p.status = 'ok';
      p.last_check_at = new Date().toISOString();
    }
  }
  mockProviders.push(p);
  if (p.status === 'ok') mockSyncModels(p);
  return mockPublicProvider(p);
}
async function mockPatchProvider(id, body) {
  await delay(params.get('probe') === 'slow' ? 2500 : 200);
  const p = mockProviderById(id);
  mockCall('patchProvider', id, body);
  if (!body || !Object.keys(body).length || Object.keys(body).some((k) => !['name', 'base_url', 'secret', 'status', 'force', 'allow_private'].includes(k))) mockFail(400, 'invalid');
  if ('allow_private' in body && mockCurrentUser().role !== 'admin') mockFail(403, 'forbidden', 'allow_private is admin only');
  if (body.force && !body.secret) mockFail(400, 'invalid', 'force requires secret');
  if ('base_url' in body) {
    if (p.kind === 'cli_subscription') mockFail(400, 'invalid');
    if (typeof body.base_url !== 'string' || !body.base_url.trim()) mockFail(400, 'invalid_base_url', 'base_url is empty');
    if (!('secret' in body)) mockFail(400, 'invalid', 'secret required when changing base_url');
  }
  if ('name' in body) {
    if (typeof body.name !== 'string' || !body.name.trim()) mockFail(400, 'invalid');
    if (mockProviders.some((x) => x !== p && x.name === body.name.trim())) mockFail(409, 'conflict');
  }
  if ('status' in body && body.status !== 'disabled') mockFail(400, 'invalid');
  if ('secret' in body) {
    if (p.kind === 'cli_subscription' || typeof body.secret !== 'string' || !body.secret) mockFail(400, 'invalid');
    const url = 'base_url' in body ? body.base_url.trim() : p.base_url;
    if (p._reapproval && !('base_url' in body)) mockFail(422, 'invalid_base_url', 'адрес изменился, нужно повторное одобрение');
    const address = url ? mockAddressKind(url) : 'public';
    if (address === 'forbidden') mockFail(422, 'invalid_base_url', 'address is not allowed');
    const changed = 'base_url' in body && url !== p.base_url;
    if (address === 'private' && (changed || !p.allow_private) && !(mockCurrentUser().role === 'admin' && body.allow_private === true)) {
      // Новый внутренний адрес: пробный запрос не уходит, провайдер ждёт администратора, прежний ключ остаётся.
      p.base_url = url;
      p.allow_private = false;
      delete p.allow_private_ips;
      p.status = 'pending_admin';
      p.last_error = null;
      return mockPublicProvider(p);
    }
    if (body.force !== true) mockProbe(body.secret, url);
    if ('base_url' in body) { p.base_url = url; if (changed) { p.allow_private = false; delete p.allow_private_ips; } }
    p.has_secret = true;
    p.secret_tail = mockSecretTail(body.secret);
    p._keyBad = false;
    p._down = false;
    p._notApi = false;
    p._reapproval = false;
    p.last_error = null;
    if (p.status !== 'disabled') p.status = body.force === true ? 'unchecked' : 'ok';
    if (body.force !== true) { p.last_check_at = new Date().toISOString(); mockSyncModels(p); }
  }
  if ('name' in body) p.name = body.name.trim();
  if ('status' in body) p.status = 'disabled';
  return mockPublicProvider(p);
}
async function mockCheckProvider(id) {
  await delay(700);
  const p = mockProviderById(id);
  mockCall('checkProvider', id);
  if (p.status === 'pending_admin') return mockPublicProvider(p);
  if (p.status === 'disabled') p.status = 'new';
  return mockPublicProvider(mockRunCheck(p));
}
async function mockListProviderRequests() {
  await delay(80);
  mockRequireAdmin();
  if (MOCK_REQUESTS_MODE === 'fail' && !mockRequestsFailed) { mockRequestsFailed = true; mockFail(500, 'invalid', 'internal'); }
  return clone(mockRequests);
}
// PATCH /api/providers/{id}/allow-private (§11): одобряется ровно тот набор IP, который админ видел в списке.
async function mockAllowPrivate(id, body) {
  await delay(300);
  mockRequireAdmin();
  mockCall('allowPrivate', id, body);
  if (!body || typeof body.allow !== 'boolean') mockFail(400, 'invalid', 'allow is required');
  if (body.allow) {
    const okIps = Array.isArray(body.ips) && body.ips.length > 0 && body.ips.length <= 64 && body.ips.every((x) => typeof x === 'string');
    if (typeof body.base_url !== 'string' || !body.base_url || !okIps) mockFail(400, 'invalid', 'base_url and ips are required');
  } else if ('ips' in body) mockFail(400, 'invalid', 'ips with allow=false');
  const request = mockRequests.find((r) => r.id === id);
  const own = mockProviders.find((x) => x.id === id);
  if (!request && !own) mockFail(404, 'not_found');
  const target = request || { base_url: own.base_url, name: own.name, created_at: own.created_at, email: mockCurrentUser().email, resolved_ips: own.allow_private_ips || [] };
  if (body.allow) {
    if (MOCK_REQUESTS_MODE === 'stale' && request && !mockStaleDone) {
      // Между показом и отправкой имя стало указывать на другой IP.
      mockStaleDone = true;
      request.resolved_ips = ['192.168.1.77'];
      mockFail(409, 'conflict', 'addresses changed');
    }
    const same = body.base_url === target.base_url && [...body.ips].sort().join(',') === [...target.resolved_ips].sort().join(',');
    if (!same) mockFail(409, 'conflict', 'addresses changed');
    const status = /down/i.test(target.base_url) ? 'error' : 'ok';
    if (request) mockRequests.splice(mockRequests.indexOf(request), 1);
    if (own) {
      Object.assign(own, { allow_private: true, allow_private_ips: [...body.ips], _reapproval: false, status, last_error: status === 'error' ? 'ConnectError' : null });
      if (status === 'ok') mockSyncModels(own);
    }
    return { id, name: target.name, base_url: target.base_url, created_at: target.created_at, email: target.email, status, allow_private: true, allow_private_ips: [...body.ips] };
  }
  // Отказ или отзыв: флаг и набор сняты, провайдер с внутренним адресом остаётся в pending_admin.
  if (request) Object.assign(request, { allow_private: false, approved_ips: [], reapproval: false });
  if (own) { Object.assign(own, { allow_private: false, _reapproval: false, status: 'pending_admin', last_error: null }); delete own.allow_private_ips; }
  return { id, name: target.name, base_url: target.base_url, created_at: target.created_at, email: target.email, status: 'pending_admin', allow_private: false, allow_private_ips: [] };
}
async function mockDeleteProvider(id) {
  await delay(200);
  const p = mockProviderById(id);
  for (const bot of mockBots) {
    if (bot.provider_id === p.id) { bot.status = 'no_model'; bot.status_label = undefined; bot.status_kind = undefined; bot.provider_id = null; bot.model_id = null; }
  }
  for (let i = mockModels.length - 1; i >= 0; i--) if (mockModels[i].provider_id === p.id) mockModels.splice(i, 1);
  mockProviders.splice(mockProviders.indexOf(p), 1);
  return { ok: true };
}
async function mockPatchModel(id, body) {
  await delay(120);
  const m = mockModels.find((x) => x.id === id);
  if (!m) mockFail(404, 'not_found');
  if (!body || Object.keys(body).some((k) => !['enabled', 'display_name'].includes(k)) || ('enabled' in body && typeof body.enabled !== 'boolean')) mockFail(400, 'invalid');
  if ('enabled' in body) { m.enabled = body.enabled; m.manually_disabled = !body.enabled; }
  if ('display_name' in body) m.display_name = body.display_name;
  return clone(m);
}
// Привязка бота к модели реестра: те же отказы, что у ядра (модель включена, провайдер в порядке, нет активной задачи).
function mockBind(bot, providerId, modelId) {
  const provider = mockProviders.find((p) => p.id === providerId);
  const model = mockModels.find((m) => m.id === modelId && m.provider_id === providerId && m.enabled);
  if (!provider || !model || provider.status !== 'ok' || provider.kind === 'google_api') mockFail(400, 'invalid', 'provider/model unavailable');
  if ((bot.provider_id !== providerId || bot.model_id !== modelId) && bot.status === 'running') mockFail(409, 'conflict', 'bot has an active turn');
  bot.provider_id = providerId;
  bot.model_id = modelId;
  bot.provider = provider.kind === 'cli_subscription' ? MOCK_CLI_RUNNER[provider.cli] : MOCK_RUNNER[provider.kind];
  bot.model = model.name;
}
async function mockDeleteBot(id) {
  await delay(300);
  const i = mockBots.findIndex((b) => b.id === id);
  if (i < 0) mockFail(404, 'not_found');
  if (mockThreads[id]) mockFail(409, 'conflict', 'bot has related records');
  mockBots.splice(i, 1);
  return { ok: true };
}
async function mockRecreateBot(id) {
  await delay(700);
  const bot = mockBots.find((b) => b.id === id);
  if (!bot) mockFail(404, 'not_found');
  bot.status = 'idle';
  bot.need_restart = false;
  bot.recreate_url = null;
  bot.status_label = undefined;
  bot.status_kind = undefined;
  return { ok: true, container: 'started' };
}

// Поддельный WebSocket терминала входа. Вывод повторяет фрагменты настоящих CLI (ANSI-цвета, гиперссылка OSC 8 с
// адресом, перенесённым по строкам, код устройства codex). Два сценария (docs/contracts.md §12):
//   claude и agy (код с сайта): ссылка, затем «Paste code here»; код `ok` даёт успех, `bad` ошибку.
//   codex (код с экрана): ссылка и код устройства, ввода нет. Тест играет роль сайта через
//   window.__loginMock.approve() (код принят, успех) и window.__loginMock.expire() (код просрочен).
// Режимы &login=: busy, forbidden, revoked, start, lost, timeout (сбои соединения); silent (терминал молчит);
// nocode (codex печатает ссылку без кода); foreign (ссылка на чужой хост вместо настоящей); mixed (чужая ссылка
// после настоящей). Печатные символы ввода эхом возвращаются как «*», управляющие клавиши как <Esc>, <Tab>, <Up>, <C-c>.
// window.__loginMock.frames хранит только управляющие JSON-кадры клиента (resize, close), вводимые данные не пишутся.
const MOCK_LOGIN_FIRST_OUTPUT_MS = 2500;
const MOCK_KEY_NAMES = { '\x1b': '<Esc>', '\t': '<Tab>', '\x1b[A': '<Up>', '\x1b[B': '<Down>', '\x1b[C': '<Right>', '\x1b[D': '<Left>' };
export const MOCK_CLAUDE_URL = 'https://claude.com/cai/oauth/authorize?code=true&client_id=9d1c250a-e61b-44d9-88ed-5944d1962f5e&response_type=code&redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback&scope=org%3Acreate_api_key+user%3Aprofile+user%3Ainference&code_challenge=mock-challenge&code_challenge_method=S256&state=mock-state';
export const MOCK_AGY_URL = 'https://accounts.google.com/o/oauth2/auth?client_id=mock-agy.apps.googleusercontent.com&response_type=code&scope=openid+email&redirect_uri=urn%3Aietf%3Awg%3Aoauth%3A2.0%3Aoob&state=mock-state';
const MOCK_EVIL_LINE = 'More info: https://evil.example/login?next=claude.ai\r\n';
// Терминал переносит длинный адрес по строкам жёстко (\r\n посреди ссылки), поэтому целый адрес есть только в OSC 8.
const mockWrap = (text, width) => text.match(new RegExp(`.{1,${width}}`, 'g')).join('\r\n');
const mockOsc8 = (url, width) => `\x1b]8;;${url}\x1b\\${mockWrap(url, width)}\x1b]8;;\x1b\\`;
// Куски вывода по CLI. Гиперссылка claude нарочно разрезана посреди адреса на два кадра, код codex посреди кода:
// недописанные ссылку и код брать нельзя.
function mockLoginChunks(cli, mode) {
  if (mode === 'silent') return [];
  if (cli === 'codex') {
    const intro = '\r\nWelcome to Codex [v0.156.1]\r\n\x1b[90mOpenAI\'s command-line coding agent\x1b[0m\r\n\r\nFollow these steps to sign in with ChatGPT using device code authorization:\r\n\r\n';
    const host = mode === 'foreign' ? 'evil.example' : 'auth.openai.com';
    const step1 = `1. Open this link in your browser and sign in to your account\r\n   \x1b[94mhttps://${host}/codex/device\x1b[0m\r\n\r\n`;
    if (mode === 'nocode') return [intro, step1];
    return [intro, step1, '2. Enter this one-time code \x1b[90m(expires in 15 minutes)\x1b[0m\r\n   \x1b[94mABCD-12', '345\x1b[0m\r\n\r\n\x1b[90mDevice codes are a common phishing target. Never share this code.\x1b[0m\r\n'];
  }
  const foreign = mode === 'foreign';
  if (cli === 'agy') {
    return ['To sign in, open this URL in your browser:\r\n', foreign ? `${MOCK_EVIL_LINE}` : `${mockOsc8(MOCK_AGY_URL, 78)}\r\n`,
      ...(mode === 'mixed' ? [MOCK_EVIL_LINE] : []), '\r\nPaste the authorization code here > '];
  }
  const link = mockOsc8(MOCK_CLAUDE_URL, 76);
  const mid = 60; // разрез внутри адреса OSC 8: терминатора нет, ссылку брать нельзя до второго кадра
  return [
    'Opening browser to sign in…\r\n',
    foreign ? `If the browser didn't open, visit: ${MOCK_EVIL_LINE}` : `If the browser didn't open, visit: \r\n${link.slice(0, mid)}`,
    ...(foreign ? [] : [`${link.slice(mid)}\r\n`]),
    ...(mode === 'mixed' ? [MOCK_EVIL_LINE] : []),
    '\r\nPaste code here if prompted > ',
  ];
}
class MockLoginSocket {
  constructor(providerId) {
    this.readyState = 0;
    this.binaryType = 'arraybuffer';
    this.onopen = this.onmessage = this.onclose = this.onerror = null;
    this.provider = mockProviders.find((p) => p.id === providerId) || null;
    this.line = '';
    window.__loginMock = window.__loginMock || { frames: [] };
    window.__loginMock.socket = this;
    // Для e2e: соединение молча обрывается, как у свёрнутой страницы на телефоне (onclose не вызывается).
    window.__loginMock.silentDrop = () => { if (window.__loginMock.socket) window.__loginMock.socket.readyState = 3; };
    // codex: человек ввёл код устройства на сайте (approve) или код просрочился (expire); CLI завершается сам.
    window.__loginMock.approve = () => { const s = window.__loginMock.socket; if (s) s.deviceResult(true); };
    window.__loginMock.expire = () => { const s = window.__loginMock.socket; if (s) s.deviceResult(false); };
    setTimeout(() => this.start(), 250);
  }
  out(text) {
    if (this.readyState !== 1 || !this.onmessage) return;
    this.onmessage({ data: new TextEncoder().encode(text).buffer });
  }
  finish(code) {
    if (this.readyState === 3) return;
    this.readyState = 3;
    if (this.onclose) this.onclose({ code, wasClean: code === 1000 });
  }
  start() {
    const mode = MOCK_LOGIN_MODE;
    if (!this.provider || this.provider.kind !== 'cli_subscription' || mode === 'forbidden') return this.finish(4404);
    if (mode === 'busy') return this.finish(4409);
    if (mode === 'revoked') return this.finish(4401);
    if (mode === 'start') return this.finish(1011);
    this.readyState = 1;
    if (this.onopen) this.onopen({});
    const cli = this.provider.cli;
    const lines = mockLoginChunks(cli, mode);
    // Первый вывод приходит не сразу: шаг 1 («жду ссылку») должен продержаться дольше шага опроса в тестах и быть виден глазами.
    lines.forEach((text, i) => setTimeout(() => this.out(text), MOCK_LOGIN_FIRST_OUTPUT_MS + 60 * (i + 1)));
    if (mode === 'lost') setTimeout(() => this.finish(1006), MOCK_LOGIN_FIRST_OUTPUT_MS + 700);
    if (mode === 'timeout') setTimeout(() => this.finish(1001), MOCK_LOGIN_FIRST_OUTPUT_MS + 700);
  }
  send(data) {
    if (this.readyState !== 1) return;
    if (typeof data === 'string') {
      let frame = null;
      try { frame = JSON.parse(data); } catch { /* не JSON */ }
      if (frame && typeof frame === 'object') window.__loginMock.frames.push(frame);
      if (frame && frame.t === 'close') this.finish(1000);
      return;
    }
    const text = new TextDecoder().decode(data instanceof ArrayBuffer ? new Uint8Array(data) : data);
    if (MOCK_KEY_NAMES[text]) { this.out(MOCK_KEY_NAMES[text]); return; }
    if (text.length === 1 && text.charCodeAt(0) < 32 && text !== '\r' && text !== '\n') {
      this.out(`<C-${String.fromCharCode(text.charCodeAt(0) + 96)}>`);
      return;
    }
    for (const ch of text) {
      if (ch === '\r' || ch === '\n') this.submit();
      else { this.line += ch; this.out('*'); }
    }
  }
  deviceResult(ok) {
    if (this.readyState !== 1 || !this.provider || this.provider.cli !== 'codex') return;
    if (ok) {
      this.out('\r\nSuccessfully logged in\r\n$ ');
      setTimeout(() => this.exit(0), 150);
    } else {
      this.out('\r\nError logging in with device code: device auth timed out after 15 minutes\r\n');
      setTimeout(() => this.exit(1), 150);
    }
  }
  submit() {
    const code = this.line;
    this.line = '';
    this.out('\r\n');
    if (this.provider && this.provider.cli === 'codex') return; // у codex ввода кода нет: решает сайт
    if (code === 'ok') {
      this.out('Login successful.\r\n$ ');
      setTimeout(() => this.exit(0), 150);
    } else if (code === 'bad') {
      this.out('OAuth error: invalid_grant\r\n');
      setTimeout(() => this.exit(1), 150);
    } else this.out('Invalid code, try again\r\nPaste code here if prompted > ');
  }
  exit(code) {
    if (this.readyState !== 1) return;
    if (this.onmessage) this.onmessage({ data: JSON.stringify({ t: 'exit', code }) });
    // Ядро проверяет вход отдельно после выхода: статус и список моделей обновляются с небольшой задержкой.
    setTimeout(() => {
      const p = this.provider;
      if (!p) return;
      if (code === 0) {
        p._loggedIn = true;
        mockRunCheck(p);
        for (const bot of mockBots) {
          if (bot.provider_id !== p.id) continue;
          if (bot.status === 'running') bot.need_restart = true;
          else { bot.need_restart = false; if (bot.status === 'error_starting') bot.status = 'idle'; }
        }
      } else mockRunCheck(p);
    }, 500);
    setTimeout(() => this.finish(1000), 80);
  }
  close() { this.finish(1000); }
}

// ---------------------------------------------------------------------------
// Мок процедур (docs/contracts.md §14). Параметры адреса: &procedures=none (процедур нет), &procedures=fail (список не
// загрузился с первого раза), &procedures=slow (долгая загрузка), &replay=off (запуск отвечает 501 not_implemented),
// &runs=manual (запуск не идёт сам: статус задаёт тест через window.__procMock.setRun), &secrets=off (GET /secrets
// недоступен: имя секрета вводится вручную), &actions=turn (в треде Скаута есть завершённый turn с действиями браузера).
// Вызовы записи пишутся в window.__procedureCalls: {name, id, body}. Значения секретов не хранятся даже в моке.
// ---------------------------------------------------------------------------
const MOCK_PROC_MODE = params.get('procedures') || '';
const MOCK_REPLAY_OFF = params.get('replay') === 'off';
const MOCK_RUNS_MANUAL = params.get('runs') === 'manual';
const MOCK_SECRETS_OFF = params.get('secrets') === 'off';
const PROC_ACTIVE = ['queued', 'running', 'waiting_approval', 'waiting_model', 'waiting_human'];
const PROC_RANK = { none: 0, other: 1, send: 2, push: 3, exec: 4, delete: 5, login: 6, pay: 7 };  // порядок ядра
const PROC_FORMAT = 'bothub-procedure/1';
const PROC_ACTIONS = ['navigate', 'click', 'fill', 'press', 'select', 'wait', 'assert'];
const mockProcStep = (id, action, target, extra = {}) => ({
  id, action, target, value: null, secret_ref: null, precondition: null, expect: null, safe_to_retry: false, risk: 'none', ...extra,
});
const mockProcs = MOCK_PROC_MODE === 'none' ? [] : [
  {
    id: 'pr1', bot_id: 'scout', name: 'Проверка входящих в Gmail', description: 'Входит в почту и открывает папку входящих.',
    source: 'human', status: 'active', version: 3, created_at: minutesAgo(60 * 24 * 6), updated_at: minutesAgo(60 * 24 * 2),
    params: [
      { name: 'email', type: 'string', required: true, default: null, secret: false },
      { name: 'folder', type: 'string', required: false, default: 'Входящие', secret: false },
    ],
    steps: [
      mockProcStep('s1', 'navigate', { url: 'https://mail.google.com/' }, { safe_to_retry: true }),
      mockProcStep('s2', 'fill', { role: 'textbox', name: 'Email' }, { value: '{{email}}', safe_to_retry: true, expect: { visible: { role: 'button', name: 'Далее' } } }),
      mockProcStep('s3', 'click', { role: 'button', name: 'Далее' }, { precondition: { url_matches: '^https://accounts\\.google\\.com' } }),
      mockProcStep('s4', 'fill', { role: 'textbox', name: 'Пароль' }, { secret_ref: 'vault:gmail', risk: 'login' }),
      mockProcStep('s5', 'click', { role: 'button', name: 'Войти' }, { risk: 'login', expect: { url_matches: '^https://mail\\.google\\.com', timeout_ms: 8000 } }),
      mockProcStep('s6', 'click', { role: 'link', name: '{{folder}}' }, { safe_to_retry: true, expect: { text: 'Входящие' } }),
    ],
  },
  {
    id: 'pr2', bot_id: 'scout', name: 'Отклик на вакансию', description: '',
    source: 'bot', status: 'draft', version: 1, created_at: minutesAgo(90), updated_at: minutesAgo(90),
    params: [{ name: 'cover', type: 'string', required: false, default: 'Здравствуйте!', secret: false }],
    steps: [
      mockProcStep('s1', 'navigate', { url: 'https://jobs.example.eu/812' }, { safe_to_retry: true }),
      mockProcStep('s2', 'click', { role: 'button', name: 'Откликнуться' }),
      mockProcStep('s3', 'fill', { role: 'textbox', name: 'Телефон' }, { needs_value: true }),
      mockProcStep('s4', 'click', { role: 'button', name: 'Отправить отклик' }, { risk: 'send' }),
    ],
  },
  {
    id: 'pr3', bot_id: 'sre', name: 'Старая проверка статуса', description: 'Открывает страницу статуса и ищет слово «OK».',
    source: 'import', status: 'archived', version: 1, created_at: minutesAgo(60 * 24 * 30), updated_at: minutesAgo(60 * 24 * 20),
    params: [],
    steps: [
      mockProcStep('s1', 'navigate', { url: 'https://status.example.org/' }, { safe_to_retry: true }),
      mockProcStep('s2', 'assert', { role: 'heading', name: 'Все системы работают' }, { safe_to_retry: true }),
    ],
  },
];
const mockSnapshot = (id) => clone(mockProcs.find((x) => x.id === id).steps);
const mockProcRuns = MOCK_PROC_MODE === 'none' ? [] : [
  { id: 'run1', procedure_id: 'pr1', procedure_version: 3, bot_id: 'scout', thread_id: 't-scout', turn_id: null, status: 'done', params: { email: 'admin@example.org' }, steps: mockSnapshot('pr1'),
    next_step: 6, step_log: ['s1', 's2', 's3', 's4', 's5', 's6'].map((id, i) => ({ step_id: id, status: 'ok', at: minutesAgo(60 * 20 - i), duration_ms: 900 + i * 310 })),
    error: null, started_at: minutesAgo(60 * 20), finished_at: minutesAgo(60 * 20 - 1), created_at: minutesAgo(60 * 20) },
  { id: 'run3', procedure_id: 'pr3', procedure_version: 1, bot_id: 'sre', thread_id: 't-sre', turn_id: null, status: 'failed', params: {}, steps: mockSnapshot('pr3'),
    next_step: 1, step_log: [{ step_id: 's1', status: 'ok', at: minutesAgo(60 * 24 * 21), duration_ms: 1400 }, { step_id: 's2', status: 'failed', at: minutesAgo(60 * 24 * 21), duration_ms: 5000, error: 'expect_failed' }],
    error: 'expect_failed', started_at: minutesAgo(60 * 24 * 21), finished_at: minutesAgo(60 * 24 * 21), created_at: minutesAgo(60 * 24 * 21) },
];
if (MOCK_RUNS_MANUAL && MOCK_PROC_MODE !== 'none') {
  // Идущий запуск без треда: опрос раз в 2 с, удаление процедуры даёт 409.
  mockProcRuns.push({ id: 'run4', procedure_id: 'pr1', procedure_version: 3, bot_id: 'scout', thread_id: null, turn_id: null, status: 'running', params: { email: 'admin@example.org' }, steps: mockSnapshot('pr1'),
    next_step: 1, step_log: [{ step_id: 's1', status: 'ok', at: minutesAgo(1), duration_ms: 1200 }], error: null, started_at: minutesAgo(1), finished_at: null, created_at: minutesAgo(1) });
}
let mockProcSeq = 100;
let mockProcListFailed = false;
const mockSecrets = [{ name: 'gmail', bot_id: null }, { name: 'jobs-site', bot_id: 'scout' }];

function mockProcCall(name, id, body) {
  (window.__procedureCalls = window.__procedureCalls || []).push({ name, id, body: body === undefined ? undefined : clone(body) });
}
// Ядро вычисляет риск шага и не даёт его понизить: здесь по подписи кнопки и по секрету.
const MOCK_SECRET_FIELD = /парол|password|passwort|пин[- ]?код|одноразов|otp|2fa|cvv|cvc|card number|номер карты|срок действия|security code|код подтверждения/i;
function mockStepFloor(s) {
  const name = String((s.target && s.target.name) || '');
  if (s.action === 'fill' && MOCK_SECRET_FIELD.test(name)) return 'login';  // как у ядра: секретное поле, а не сам secret_ref
  if (s.action !== 'click') return 'none';
  if (/оплат|купить|pay/i.test(name)) return 'pay';
  if (/удалить|delete/i.test(name)) return 'delete';
  if (/отправ|submit|send/i.test(name)) return 'send';
  if (/войти|login/i.test(name)) return 'login';
  return 'none';
}
// Итоговый риск шага, как у ядра: присланный не ниже вычисленного, не прислан или ниже: вычисленный.
function mockStepRisk(s) {
  const floor = mockStepFloor(s);
  const given = s.risk || 'none';
  return (PROC_RANK[given] ?? 0) < PROC_RANK[floor] ? floor : given;
}
function mockProcView(p) {
  const runs = mockProcRuns.filter((r) => r.procedure_id === p.id).sort((a, b) => b.created_at.localeCompare(a.created_at));
  const last = runs[0];
  const view = clone(p);
  view.steps = view.steps.map((s) => ({ ...s, computed_risk: mockStepFloor(s) }));
  view.last_run = last ? { id: last.id, status: last.status, started_at: last.started_at, finished_at: last.finished_at, error: last.error } : null;
  return view;
}
function mockProcFind(id) {
  const p = mockProcs.find((x) => x.id === id);
  if (!p) mockFail(404, 'not_found', 'procedure not found');
  return p;
}
function mockProc422(path, code, text) { mockFail(422, 'invalid', `${path}: ${code}: ${text}`); }
// Проверка при сохранении, как у ядра: detail «путь: код: короткий текст» (steps[1].value: secret_required: …). PWA переводит код.
// lenientRisk: импорт и запись из turn пересчитывают риск без ошибки.
function mockProcValidate(body, current, { lenientRisk = false } = {}) {
  const params_ = body.params !== undefined ? body.params : current.params;
  const steps = body.steps !== undefined ? body.steps : current.steps;
  if (body.name !== undefined && !String(body.name).trim()) mockProc422('name', 'empty', 'is empty');
  if (body.params !== undefined) {
    const seen = new Set();
    params_.forEach((prm, i) => {
      if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(prm.name || '')) mockProc422(`params[${i}].name`, 'invalid', 'invalid name');
      if (seen.has(prm.name)) mockProc422(`params[${i}].name`, 'duplicate', 'duplicate name');
      seen.add(prm.name);
      if (prm.secret && prm.default != null && prm.default !== '') mockProc422(`params[${i}].default`, 'not_allowed', 'a secret param stores no default');
    });
  }
  const known = new Set(params_.map((x) => x.name));
  const ids = new Set();
  steps.forEach((s, i) => {
    if (ids.has(s.id)) mockProc422(`steps[${i}].id`, 'duplicate', 'duplicate id');
    ids.add(s.id);
    if (!PROC_ACTIONS.includes(s.action)) mockProc422(`steps[${i}].action`, 'invalid', 'unknown action');
    if (s.value != null && s.secret_ref) mockProc422(`steps[${i}].value`, 'mutually_exclusive', 'value and secret_ref are mutually exclusive');
    if (s.secret_ref && !/^vault:[A-Za-z][A-Za-z0-9_-]{0,63}$/.test(s.secret_ref)) mockProc422(`steps[${i}].secret_ref`, 'invalid', 'expected vault:<name>');
    if (s.target && s.target.selector && /\{\{|\}\}/.test(s.target.selector)) mockProc422(`steps[${i}].target.selector`, 'param_in_selector', 'a param in a selector is not allowed');
    if (s.target && /[\u0000-\u001f\u007f-\u009f\u00ad\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u206f\ufeff]/.test(String(s.target.name || ''))) {
      mockProc422(`steps[${i}].target.name`, 'control_chars', 'control and invisible characters are not allowed');
    }
    for (const m of String(s.value || '').matchAll(/\{\{\s*([^}\s]+)\s*\}\}/g)) {
      if (!known.has(m[1])) mockProc422(`steps[${i}].value`, 'param_undeclared', 'param is not declared');
    }
    if (s.action === 'fill' && s.value != null && !s.needs_value && MOCK_SECRET_FIELD.test(String((s.target && s.target.name) || ''))) {
      const only = /^\s*\{\{\s*([^}\s]+)\s*\}\}\s*$/.exec(s.value);
      if (!(only && (params_.find((x) => x.name === only[1]) || {}).secret)) mockProc422(`steps[${i}].value`, 'secret_required', 'a secret field requires secret_ref');
    }
    const floor = mockStepFloor(s);
    // Риск не прислан: ядро считает его само. Прислан ниже вычисленного: 422 с путём steps[i].risk.
    if (!lenientRisk && s.risk != null && (PROC_RANK[s.risk] ?? 0) < PROC_RANK[floor]) mockProc422(`steps[${i}].risk`, 'risk_below_computed', 'risk is lower than the one computed by the core');
    for (const key of ['precondition', 'expect']) {
      const pattern = s[key] && s[key].url_matches;
      if (!pattern) continue;
      try { new RegExp(pattern); } catch { mockProc422(`steps[${i}].${key}.url_matches`, 'regex_invalid', 'invalid regex'); continue; }
      if (/\([^)]*[+*][^)]*\)[+*]/.test(pattern)) mockProc422(`steps[${i}].${key}.url_matches`, 'regex_nested', 'a repeat inside a repeated group is not allowed');
    }
  });
}
function mockProcNameTaken(name, exceptId) {
  return mockProcs.some((p) => p.id !== exceptId && p.name.trim().toLowerCase() === String(name).trim().toLowerCase());
}
function mockProcStepsFromTurn(events) {
  const out = [];
  for (const ev of events) {
    const pl = ev.payload || {};
    if (ev.kind !== 'browser_step' || ['snapshot', 'screenshot'].includes(pl.action)) continue;
    const id = `s${out.length + 1}`;
    const m = /^(\S+)\s+«(.+)»$/.exec(pl.target || '');
    const roles = { кнопка: 'button', ссылка: 'link', поле: 'textbox' };
    const target = pl.action === 'navigate' ? { url: pl.url || '' } : m ? { role: roles[m[1]] || 'button', name: m[2] } : { role: 'textbox', name: '' };
    const masked = pl.action === 'fill' && (pl.value === '[redacted]' || pl.value == null);
    const step = mockProcStep(id, pl.action, target, { safe_to_retry: pl.action === 'navigate' });
    if (masked) { step.needs_value = true; if (!String(target.name || '').trim() || MOCK_SECRET_FIELD.test(String(target.name))) step.needs_secret = true; }
    else if (pl.value != null) step.value = pl.value;
    step.risk = mockStepFloor(step);
    out.push(step);
  }
  return out;
}
function mockRunApproval(run, step, p) {
  const id = `apr-${run.id}-${step.id}`;
  if (!mockApprovals.some((a) => a.id === id)) {
    mockApprovals.push({
      id, thread_id: run.thread_id, turn_id: run.turn_id, bot_id: run.bot_id, risk: step.risk, tool: 'mcp__bothub__browser',
      title: `Процедура «${p.name}», шаг ${step.id}`, args: {}, args_hash: 'c0ffee1', status: 'pending', expires_at: minutesAgo(-20),
    });
  }
  return id;
}
// Один шаг мок-исполнителя: queued → running, дальше по шагу; шаг с подтверждением ждёт решения в approvals.
function mockRunAdvance(run) {
  if (!PROC_ACTIVE.includes(run.status)) return;
  const p = mockProcs.find((x) => x.id === run.procedure_id);
  if (!p) return;
  const nowIso = new Date().toISOString();
  if (run.status === 'queued') { run.status = 'running'; run.started_at = nowIso; return; }
  const snapshot = run.steps && run.steps.length ? run.steps : p.steps;
  const step = snapshot[run.next_step];
  if (!step) { run.status = 'done'; run.finished_at = nowIso; return; }
  if (run.status === 'waiting_approval') {
    const a = mockApprovals.find((x) => x.id === run._approval);
    if (a && a.status === 'approved') { run.status = 'running'; run._approved = run.next_step; }
    else if (a && a.status !== 'pending') { run.status = 'failed'; run.error = 'approval_rejected'; run.finished_at = nowIso; }
    return;
  }
  if (run.status !== 'running') return;
  if (step.risk && step.risk !== 'none' && run._approved !== run.next_step) {
    run._approval = mockRunApproval(run, step, p);
    run.status = 'waiting_approval';
    return;
  }
  run.step_log.push({ step_id: step.id, status: 'ok', at: nowIso, duration_ms: 700 + run.next_step * 130 });
  run.next_step += 1;
  if (run.next_step >= snapshot.length) { run.status = 'done'; run.finished_at = nowIso; }
}
function mockRunView(run) {
  const view = clone(run);
  for (const key of Object.keys(view)) if (key.startsWith('_')) delete view[key];
  return view;
}
if (MOCK) {
  if (!MOCK_RUNS_MANUAL) setInterval(() => mockProcRuns.forEach(mockRunAdvance), 1200);
  // Для e2e: статус и шаги запуска задаёт тест; seed добавляет завершённый turn с действиями браузера в тред Скаута.
  window.__procMock = {
    setRun: (id, patch) => {
      const r = mockProcRuns.find((x) => x.id === id);
      if (!r) return false;
      Object.assign(r, patch);
      const arr = mockEvents[r.thread_id];
      if (arr) arr.push({ seq: (arr[arr.length - 1]?.seq || 0) + 1, ts: new Date().toISOString(), turn_id: null, kind: 'status', actor: 'system', payload: { turn_id: null, status: r.status } });
      return true;
    },
    runs: mockProcRuns,
    procs: mockProcs,
    approvals: mockApprovals,
  };
  if (params.get('actions') === 'turn') {
    const arr = mockEvents['t-scout'];
    const base = arr[arr.length - 1].seq;
    [
      { action: 'navigate', target: '', url: 'https://jobs.example.eu/812', value: null, result: 'ok' },
      { action: 'snapshot', target: '', url: null, value: null, result: 'ok' },
      { action: 'click', target: 'кнопка «Откликнуться»', url: null, value: null, result: 'ok' },
      { action: 'fill', target: '[redacted]', url: null, value: '[redacted]', result: 'ok' },
      { action: 'click', target: 'кнопка «Отправить отклик»', url: null, value: null, result: 'ok' },
    ].forEach((payload, i) => arr.push({ seq: base + 1 + i, ts: minutesAgo(10 - i), turn_id: 'tu-form', kind: 'browser_step', actor: 'bot:scout', payload }));
    arr.push({ seq: base + 6, ts: minutesAgo(4), turn_id: 'tu-form', kind: 'assistant_msg', actor: 'bot:scout', payload: { text: 'Отклик отправлен.', final: true } });
    arr.push({ seq: base + 7, ts: minutesAgo(4), turn_id: 'tu-form', kind: 'status', actor: 'system', payload: { turn_id: 'tu-form', status: 'done' } });
  }
}

async function mockProcGate(ms = 120) {
  await delay(ms);
  mockRequireSession();
}

// ---------------------------------------------------------------------------
// Публичное API
// ---------------------------------------------------------------------------

export async function listBots() {
  if (MOCK) { await delay(); return clone(mockBots); }
  return request('/bots');
}

// ---------------------------------------------------------------------------
// Вход, инвайты, пользователи, сессии (docs/contracts.md §10)
// ---------------------------------------------------------------------------

// Первый запуск: пользователей ещё нет. Признак отдаёт GET /api/setup/status (docs/contracts.md §10); пустой POST /setup
// для этого не годится: ядро отвечает 400 на тело раньше проверки пользователей, и вход показывался вместо настройки.
export async function needsSetup() {
  if (MOCK) return !mockSetupDone;
  try {
    const res = await fetch('api/setup/status', { credentials: 'same-origin' });
    if (!res.ok) return false;
    const body = await res.json().catch(() => null);
    return !!body && body.needs_setup === true;
  } catch { return false; }
}
export async function setup(email, password, setupCode = '') {
  if (MOCK) return mockSetup(email, password, setupCode);
  const headers = setupCode ? { 'X-Setup-Code': setupCode } : {};
  return request('/setup', { method: 'POST', body: { email, password }, headers });
}
export async function authMe() {
  if (MOCK) return mockAuthMe();
  const me = await request('/auth/me');
  if (me && me.csrf_token) csrfToken = me.csrf_token;
  return me;
}
export async function login(email, password) {
  if (MOCK) return mockLogin(email, password);
  const user = await request('/auth/login', { method: 'POST', body: { email, password }, bearer: false });
  csrfToken = (user && user.csrf_token) || '';
  setToken('');
  return user;
}
export async function logout() {
  try {
    if (MOCK) await mockLogout();
    else await request('/auth/logout', { method: 'POST' });
  } finally {
    forgetCsrf();
    setToken('');
  }
}
export async function changePassword(oldPassword, newPassword) {
  if (MOCK) return mockChangePassword(oldPassword, newPassword);
  const res = await request('/auth/password', { method: 'POST', body: { old_password: oldPassword, new_password: newPassword } });
  forgetCsrf();
  setToken('');
  return res;
}
export async function createInvite(role = 'member', days = 7) {
  if (MOCK) return mockCreateInvite(role, days);
  return request('/invites', { method: 'POST', body: { role, days } });
}
export async function listInvites() {
  if (MOCK) { await delay(); mockRequireAdmin(); return clone(mockInvites); }
  return request('/invites');
}
export async function checkInvite(token) {
  if (MOCK) { await delay(); return { valid: MOCK_VALID_TOKENS.has(token) }; }
  return request(`/invites/check?token=${encodeURIComponent(token)}`, { bearer: false });
}
export async function acceptInvite(token, email, password) {
  if (MOCK) return mockAcceptInvite(token, email, password);
  return request('/invites/accept', { method: 'POST', body: { token, email, password }, bearer: false });
}
export async function deleteInvite(id) {
  if (MOCK) return mockDeleteInvite(id);
  return request(`/invites/${encodeURIComponent(id)}`, { method: 'DELETE' });
}
export async function listUsers() {
  if (MOCK) { await delay(); mockRequireAdmin(); return clone(mockUsers); }
  return request('/users');
}
export async function patchUser(id, body) {
  if (MOCK) return mockPatchUser(id, body);
  return request(`/users/${encodeURIComponent(id)}`, { method: 'PATCH', body });
}
export async function listSessions() {
  if (MOCK) { await delay(); mockRequireSession(); return clone(mockSessions); }
  return request('/sessions');
}
export async function deleteSession(id) {
  if (MOCK) return mockDeleteSession(id);
  return request(`/sessions/${encodeURIComponent(id)}`, { method: 'DELETE' });
}

// Создание бота (docs/contracts.md §9, «Запуск бота после ответа»): ядро отвечает сразу, 201 со status starting, контейнер
// поднимается фоном. Ответ всё равно может пропасть по дороге (сеть, 502 и 504 от прокси): бот при этом, скорее всего, уже
// создан. Тогда список ботов перечитывается и бот ищется по имени и времени создания; найден: он и есть результат,
// нет: прежняя ошибка (экран предложит «Повторить»).
export const CREATE_CLOCK_SKEW_MS = 60 * 1000; // часы сервера могут отставать от часов клиента
export function isLostResponse(err) {
  if (err instanceof ApiError) return err.status === 502 || err.status === 504;
  // fetch без ответа: Chrome пишет «Failed to fetch», Safari «Load failed», Firefox «NetworkError…».
  return err instanceof TypeError && /fetch|network|load failed/i.test(err.message || '');
}
export function findCreatedBot(bots, body, startedAt) {
  const since = startedAt - CREATE_CLOCK_SKEW_MS;
  const stamp = (bot) => Date.parse(bot.created_at);
  const hits = (Array.isArray(bots) ? bots : []).filter((bot) => bot && bot.name === body.name && stamp(bot) >= since);
  hits.sort((a, b) => stamp(b) - stamp(a));
  return hits[0] || null;
}
export async function createBotRecovering(post, list, body, now = Date.now) {
  const startedAt = now();
  try {
    return await post();
  } catch (err) {
    if (!isLostResponse(err)) throw err;
    let bots;
    try { bots = await list(); } catch { throw err; }
    const found = findCreatedBot(bots, body, startedAt);
    if (found) return found;
    throw err;
  }
}
// ?mock=1&create=starting|lost|down: создание с запуском в фоне (через 3 с idle), ответ потерян после создания, ответ потерян без создания.
const MOCK_CREATE = params.get('create') || '';
async function mockCreateBot(body) {
  await delay();
  if (MOCK_CREATE === 'down') throw new TypeError('Failed to fetch');
  const starting = MOCK_CREATE === 'starting' || MOCK_CREATE === 'lost';
  const bot = { status: starting ? 'starting' : 'idle', status_label: 'Готово', status_kind: 'neutral', summary: '', location: body.executor === 'mac' ? 'Mac' : 'Сервер', budget_daily_tokens: 200000, auto_allow: [], role: '', need_restart: false, provider_id: null, model_id: null, auto_compact_percent: 80, created_at: new Date().toISOString(), ...body, container: starting ? 'starting' : 'started' };
  if (starting) { delete bot.status_label; delete bot.status_kind; }
  if (body.provider_id || body.model_id) mockBind(bot, body.provider_id, body.model_id);
  mockBots.push(bot);
  if (starting) setTimeout(() => { bot.status = 'idle'; }, 3000);
  if (MOCK_CREATE === 'lost') throw new TypeError('Failed to fetch');
  return clone(bot);
}
export async function createBot(body) {
  const post = MOCK ? () => mockCreateBot(body) : () => request('/bots', { method: 'POST', body });
  return createBotRecovering(post, () => listBots(), body);
}
// Конструктор ботов (docs/contracts.md §9): черновик из описания владельца, задержка ~1.5с как у настоящего вызова Claude.
export async function createBotDraft(description) {
  if (MOCK) { await delay(1500); return clone(buildMockDraft(description)); }
  return request('/bots/draft', { method: 'POST', body: { description } });
}
export async function patchBot(id, body) {
  if (MOCK) {
    await delay();
    const b = mockBots.find((x) => x.id === id);
    if (!b) mockFail(404, 'not_found');
    const { provider_id: providerId, model_id: modelId, ...rest } = body;
    if ('auto_compact_percent' in rest && rest.auto_compact_percent !== null && !(Number.isInteger(rest.auto_compact_percent) && rest.auto_compact_percent >= 50 && rest.auto_compact_percent <= 95)) mockFail(400, 'invalid', 'auto_compact_percent: 50..95 or null');
    if (providerId !== undefined || modelId !== undefined) mockBind(b, providerId ?? b.provider_id, modelId ?? b.model_id);
    Object.assign(b, rest);
    // Как на сервере: при смене провайдера модель становится моделью этого провайдера по умолчанию.
    if (rest.provider && !rest.model) b.model = MOCK_DEFAULT_MODEL[rest.provider] || b.model;
    return clone(b);
  }
  return request(`/bots/${id}`, { method: 'PATCH', body });
}
// Удаление бота без истории (docs/contracts.md §10): при тредах, памяти, расходе или расписаниях ядро отвечает 409.
export async function deleteBot(id) {
  if (MOCK) return mockDeleteBot(id);
  return request(`/bots/${encodeURIComponent(id)}`, { method: 'DELETE' });
}
// Пересоздание компьютера бота: файлы остаются, статус возвращается в idle.
export async function recreateBot(id) {
  if (MOCK) return mockRecreateBot(id);
  return request(`/bots/${encodeURIComponent(id)}/recreate`, { method: 'POST' });
}

// ---------------------------------------------------------------------------
// Реестр провайдеров и моделей, вход по подписке (docs/contracts.md §11–12)
// ---------------------------------------------------------------------------
// quiet: фоновое чтение для подписей (имя провайдера и его ошибка у ботов); в моке не участвует в сбоях и задержках экрана провайдеров.
export async function listProviders({ quiet = false } = {}) {
  if (MOCK) { if (quiet) await delay(20); else await mockListGate(); mockRequireSession(); return mockProviders.map(mockPublicProvider); }
  return request('/providers');
}
// body: {kind, name, secret?, base_url?, cli?}. Ядро сразу делает пробный запрос и отвечает записью со status ok или error.
export async function createProvider(body) {
  if (MOCK) return mockCreateProvider(body);
  return request('/providers', { method: 'POST', body });
}
export async function patchProvider(id, body) {
  if (MOCK) return mockPatchProvider(id, body);
  return request(`/providers/${encodeURIComponent(id)}`, { method: 'PATCH', body });
}
export async function listProviderRequests() {
  if (MOCK) return mockListProviderRequests();
  return request('/admin/provider-requests');
}
export async function allowPrivateProvider(id, body) {
  if (MOCK) return mockAllowPrivate(id, body);
  return request(`/providers/${encodeURIComponent(id)}/allow-private`, { method: 'PATCH', body });
}
export async function checkProvider(id) {
  if (MOCK) return mockCheckProvider(id);
  return request(`/providers/${encodeURIComponent(id)}/check`, { method: 'POST' });
}
export async function deleteProvider(id) {
  if (MOCK) return mockDeleteProvider(id);
  return request(`/providers/${encodeURIComponent(id)}`, { method: 'DELETE' });
}
export async function listModels(includeDisabled = false) {
  if (MOCK) {
    await mockListGate();
    mockRequireSession();
    return clone(mockModels.filter((m) => includeDisabled || m.enabled));
  }
  return request(includeDisabled ? '/models?include_disabled=true' : '/models');
}
export async function patchModel(id, body) {
  if (MOCK) return mockPatchModel(id, body);
  return request(`/models/${encodeURIComponent(id)}`, { method: 'PATCH', body });
}
// ---------------------------------------------------------------------------
// Браузер бота (docs/contracts.md §13). Мок: состояние управления, шаги, перехват с задержкой, поддельный экранный сокет.
// Параметр &browser= : busy (экран открыт на другом, 4409), down (компьютер не запущен, 1013), lost (связь рвётся после
// подключения), denied (нет доступа, 4404), none (у бота нет браузера), conflict (перехват даёт 409, сервер уже в human),
// bad (перехват даёт 502), hold (возврат боту ждёт window.__browserMock.snapshot()), ask (бот ждёт подтверждения),
// empty (шагов ещё не было), refused (отказ до рукопожатия без кода причины).
// Значение скрытого ввода нигде не хранится: в window.__browserMock.secrets остаются только длина и имя секрета.
// ---------------------------------------------------------------------------
export const MOCK_BROWSER_MODE = params.get('browser') || '';
const mockBrowserStates = new Map();
const mockBrowserBot = (botId) => {
  const bot = mockBots.find((b) => b.id === botId);
  if (!bot || MOCK_BROWSER_MODE === 'denied') mockFail(404, 'not_found');
  return bot;
};
function mockBrowserEntry(botId) {
  if (!mockBrowserStates.has(botId)) mockBrowserStates.set(botId, { state: 'bot', since: minutesAgo(3), by: null });
  return mockBrowserStates.get(botId);
}
let mockBrowserSeq = 0;
function mockBrowserEvent(botId, kind, payload, actor = 'owner') {
  const thread = mockThreads[botId];
  if (!thread) return;
  const arr = mockEvents[thread.id] || (mockEvents[thread.id] = []);
  const seq = (arr[arr.length - 1]?.seq || 0) + 1;
  arr.push({ seq, ts: new Date().toISOString(), turn_id: 'tu2', kind, actor, payload });
}
function mockBrowserExpireApprovals(botId) {
  for (const a of mockApprovals) if (a.bot_id === botId && a.status === 'pending' && /browser/.test(a.tool)) a.status = 'expired';
}
// Первый успешный снимок после возврата: returning → bot (раздел 13).
function mockBrowserSnapshot(botId = 'sre') {
  const e = mockBrowserEntry(botId);
  if (e.state !== 'returning') return;
  e.state = 'bot';
  e.since = new Date().toISOString();
  mockBrowserEvent(botId, 'browser_control', { from: 'returning', to: 'bot', by: e.by, reason: 'snapshot' }, `bot:${botId}`);
  mockBrowserEvent(botId, 'browser_step', { action: 'snapshot', target: '', url: null, value: null, result: 'ok' }, `bot:${botId}`);
}
function mockBrowserSeed() {
  if (MOCK_BROWSER_MODE !== 'empty') mockEvents['t-sre'].push(
    { seq: 6, ts: minutesAgo(5), turn_id: 'tu2', kind: 'browser_step', actor: 'bot:sre', payload: { action: 'navigate', target: '', url: 'https://status.example.org/', value: null, result: 'ok' } },
    { seq: 7, ts: minutesAgo(5), turn_id: 'tu2', kind: 'browser_step', actor: 'bot:sre', payload: { action: 'snapshot', target: '', url: null, value: null, result: 'ok' } },
    { seq: 8, ts: minutesAgo(4), turn_id: 'tu2', kind: 'browser_step', actor: 'bot:sre', payload: { action: 'click', target: 'ссылка «webapp»', url: null, value: null, result: 'ok' } },
    { seq: 9, ts: minutesAgo(3), turn_id: 'tu2', kind: 'browser_step', actor: 'bot:sre', payload: { action: 'fill', target: '[redacted]', url: null, value: '[redacted]', result: 'ok' } },
    { seq: 10, ts: minutesAgo(2), turn_id: 'tu2', kind: 'browser_step', actor: 'bot:sre', payload: { action: 'click', target: 'кнопка «Войти»', url: null, value: null, result: 'error' } },
  );
  if (MOCK_BROWSER_MODE === 'ask') {
    mockApprovals.push({
      id: 'apb1', thread_id: 't-sre', turn_id: 'tu2', bot_id: 'sre', risk: 'send', tool: 'mcp__bothub__browser',
      title: 'Отправить форму входа на status.example.org', args: { url: 'status.example.org/login', data: 'Логин, пароль из хранилища', reversible: false },
      args_hash: 'b7c01de', status: 'pending', expires_at: minutesAgo(-20),
    });
  }
}
if (MOCK) {
  mockBrowserSeed();
  window.__browserMock = {
    secrets: [],
    snapshot: () => mockBrowserSnapshot('sre'),
    // Сервер сменил состояние без события: клиент должен подтянуть его опросом.
    serverSet: (state) => { const e = mockBrowserEntry('sre'); e.state = state; e.since = new Date().toISOString(); },
    // Произвольный шаг (длинный адрес, ошибка): ядро присылает такие же browser_step.
    pushStep: (payload) => mockBrowserEvent('sre', 'browser_step', { target: '', url: null, value: null, result: 'ok', ...payload }, 'bot:sre'),
    pushSteps: (n, from = 100) => {
      for (let i = 0; i < n; i++) mockBrowserEvent('sre', 'browser_step', { action: 'click', target: `элемент ${from + i}`, url: null, value: null, result: 'ok' }, 'bot:sre');
    },
  };
}

export async function getBrowser(botId) {
  if (MOCK) {
    await delay(90);
    mockRequireSession();
    mockBrowserBot(botId);
    return clone(mockBrowserEntry(botId));
  }
  return request(`/bots/${encodeURIComponent(botId)}/browser`);
}
async function mockBrowserTransition(botId, action) {
  await delay(700);
  mockRequireSession();
  mockBrowserBot(botId);
  const e = mockBrowserEntry(botId);
  if (action === 'takeover' && MOCK_BROWSER_MODE === 'bad') mockFail(502, 'launcher_unavailable', 'launcher_unavailable');
  if (action === 'takeover' && MOCK_BROWSER_MODE === 'conflict') {
    if (e.state !== 'human') { e.state = 'human'; e.since = new Date().toISOString(); e.by = 'u-other'; }
    mockFail(409, 'invalid_transition', 'invalid_transition');
  }
  if (!(action === 'takeover' ? ['bot', 'returning'] : ['human']).includes(e.state)) mockFail(409, 'invalid_transition', 'invalid_transition');
  const from = e.state;
  e.state = action === 'takeover' ? 'human' : 'returning';
  e.since = new Date().toISOString();
  e.by = 'u-admin';
  mockBrowserExpireApprovals(botId);
  mockBrowserEvent(botId, 'browser_control', { from, to: e.state, by: e.by, reason: action });
  if (action === 'return' && MOCK_BROWSER_MODE !== 'hold') setTimeout(() => mockBrowserSnapshot(botId), 4000);
  return clone(e);
}
export const browserTakeover = (botId) => (MOCK ? mockBrowserTransition(botId, 'takeover') : request(`/bots/${encodeURIComponent(botId)}/browser/takeover`, { method: 'POST' }));
export const browserReturn = (botId) => (MOCK ? mockBrowserTransition(botId, 'return') : request(`/bots/${encodeURIComponent(botId)}/browser/return`, { method: 'POST' }));
// Скрытый ввод: значение уходит только в тело запроса и нигде не сохраняется на клиенте.
export async function browserSecretInput(botId, value, saveAs) {
  const body = saveAs ? { value, save_as: saveAs } : { value };
  if (!MOCK) return request(`/bots/${encodeURIComponent(botId)}/browser/secret-input`, { method: 'POST', body });
  await delay(400);
  mockRequireSession();
  mockBrowserBot(botId);
  if (mockBrowserEntry(botId).state !== 'human') mockFail(409, 'human_required', 'human_required');
  if (!window.__screenMock || !window.__screenMock.live) mockFail(409, 'screen_required', 'screen_required');
  // eslint-disable-next-line no-control-regex
  if (/[\x00-\x1f\x7f]/.test(value)) mockFail(400, 'invalid', 'secret-input contains control characters');
  window.__browserMock.secrets.push({ length: value.length, save_as: saveAs || null });
  return { ok: true };
}

// Поддельный экранный WebSocket: open, затем close с кодом ядра (4401, 4404, 4409, 4410, 1013). Для e2e:
// window.__screenMock.close(код) закрывает текущий сокет, freeBusy() освобождает экран «другого клиента».
class MockScreenSocket extends EventTarget {
  constructor(botId) {
    super();
    this.readyState = 0;
    this.botId = botId;
    const m = (window.__screenMock = window.__screenMock || { connects: 0, live: false, viewOnly: null, fit: true, keys: 0, clicks: 0, closes: [], busy: MOCK_BROWSER_MODE === 'busy' });
    m.socket = this;
    m.close = (code = 1006) => this.finish(code);
    m.freeBusy = () => { m.busy = false; };
    setTimeout(() => this.start(), 250);
  }
  start() {
    const m = window.__screenMock;
    if (this.readyState === 3) return;
    if (MOCK_BROWSER_MODE === 'denied') return this.finish(4404);
    if (MOCK_BROWSER_MODE === 'refused') return this.finish(1006); // отказ до рукопожатия: браузер не видит причину
    if (m.busy) return this.finish(4409);
    if (MOCK_BROWSER_MODE === 'down') return this.finish(1013);
    this.readyState = 1;
    m.connects += 1;
    this.dispatchEvent(new Event('open'));
    if (MOCK_BROWSER_MODE === 'lost') setTimeout(() => this.finish(1006), 900);
  }
  finish(code) {
    if (this.readyState === 3) return;
    const wasOpen = this.readyState === 1;
    this.readyState = 3;
    window.__screenMock.closes.push(code);
    const ev = new Event('close');
    ev.code = code;
    ev.wasOpen = wasOpen;
    this.dispatchEvent(ev);
  }
  close() { this.finish(1000); }
}
// Экранный WebSocket (RFB поверх WebSocket, только cookie-сессия). Коды закрытия: 4401, 4404, 4409, 4410, 1013.
export function openScreenSocket(botId) {
  if (MOCK) return new MockScreenSocket(botId);
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const base = new URL(`api/bots/${encodeURIComponent(botId)}/screen`, location.href);
  const socket = new WebSocket(`${proto}://${base.host}${base.pathname}`);
  socket.binaryType = 'arraybuffer';
  return socket;
}

// Терминал входа: бинарные кадры несут байты PTY, клиент шлёт байты ввода и JSON {t:'resize'|'close'}.
// Только cookie-сессия: токен в адрес не кладём. В моке вместо сети поддельный сокет.
export function openLoginSocket(providerId) {
  if (MOCK) return new MockLoginSocket(providerId);
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const base = new URL(`api/providers/${encodeURIComponent(providerId)}/login`, location.href);
  const socket = new WebSocket(`${proto}://${base.host}${base.pathname}`);
  socket.binaryType = 'arraybuffer';
  return socket;
}

export async function listThreads() {
  if (MOCK) { await delay(); return clone(Object.values(mockThreads)); }
  return request('/threads');
}
export async function getThreadByBot(botId) {
  if (MOCK) { await delay(); return clone(mockThreads[botId]); }
  const list = await request(`/threads?bot_id=${encodeURIComponent(botId)}`);
  return list[0];
}
// Строка треда плюс summary, auto_compact_disabled и context {tokens, window, percent, estimated, compacted_at, compactions}.
export async function getThread(id) {
  if (MOCK) {
    await delay();
    const thread = Object.values(mockThreads).find((t) => t.id === id);
    return thread ? mockThreadView(thread) : undefined;
  }
  try {
    return await request(`/threads/${encodeURIComponent(id)}`);
  } catch (err) {
    // Ядро без GET /threads/{id}: строку берём из списка, контекста тогда нет.
    if (err && (err.status === 404 || err.status === 405)) {
      const list = await request('/threads');
      return list.find((t) => t.id === id);
    }
    throw err;
  }
}
export async function createThread(botId, title = '') {
  if (MOCK) { await delay(); const t = { id: `t-${botId}`, bot_id: botId, kind: 'direct', title, dry_run: false }; mockThreads[botId] = t; return clone(t); }
  return request('/threads', { method: 'POST', body: { bot_id: botId, title } });
}
export async function patchThread(id, body) {
  if (MOCK) { await delay(); return { id, ...body }; }
  return request(`/threads/${id}`, { method: 'PATCH', body });
}

export async function getEvents(threadId, since = 0) {
  if (MOCK) {
    await delay(80);
    const all = mockEvents[threadId] || [];
    return clone(all.filter((e) => e.seq > since));
  }
  return request(`/threads/${threadId}/events?since=${since}`);
}

export async function createTurn(threadId, prompt, client = 'iphone') {
  if (MOCK) {
    await delay(300);
    const arr = mockEvents[threadId] || (mockEvents[threadId] = []);
    const seq = (arr[arr.length - 1]?.seq || 0) + 1;
    const turnId = `turn-mock-${++mockTurnSeq}`;
    mockCtxState(threadId).activeTurn = turnId;
    arr.push({ seq, kind: 'user_msg', actor: 'owner', client, payload: { text: prompt } });
    mockPushEvent(threadId, 'status', { turn_id: turnId, status: 'running' }, turnId);
    setTimeout(() => {
      const seq2 = (arr[arr.length - 1]?.seq || 0) + 1;
      arr.push({ seq: seq2, kind: 'assistant_msg', actor: 'bot:mock', payload: { text: `ok: ${prompt}`, final: true } });
    }, 500);
    setTimeout(() => mockFinishTurn(threadId, turnId), 700);
    return { id: turnId, status: 'queued', turn_type: 'chat' };
  }
  return request(`/threads/${threadId}/turns`, { method: 'POST', body: { prompt, client } });
}
// Сжатие контекста: ход turn_type 'compact' без сообщения владельца. 409: в треде идёт ход или сжимать нечего.
export async function compactThread(id) {
  if (MOCK) return mockCompactThread(id);
  return request(`/threads/${encodeURIComponent(id)}/compact`, { method: 'POST' });
}
export async function stopTurn(turnId) {
  if (MOCK) {
    await delay();
    const bot = mockBots.find((b) => `tu-${b.id}` === turnId);  // ход, который пауза не прервала: бот освобождается
    if (bot) { bot.status = 'stopped'; bot.status_label = 'Остановлен'; bot.status_kind = 'neutral'; }
    return { id: turnId, status: 'stopped' };
  }
  return request(`/turns/${turnId}/stop`, { method: 'POST' });
}

export async function listApprovals(status = 'pending') {
  if (MOCK) { await delay(); return clone(mockApprovals.filter((a) => a.status === status)); }
  return request(`/approvals?status=${status}`);
}
export async function decideApproval(id, decision, remember = false) {
  if (MOCK) {
    await delay(200);
    const a = mockApprovals.find((x) => x.id === id);
    if (a) a.status = decision === 'approve' ? 'approved' : 'rejected';
    return clone(a);
  }
  return request(`/approvals/${id}/decide`, { method: 'POST', body: { decision, remember, client: 'iphone' } });
}

export async function listMemory(filterOrBotId, qParam, statusParam) {
  let botId = null, q = null, status = null;
  if (filterOrBotId && typeof filterOrBotId === 'object') {
    botId = filterOrBotId.botId || filterOrBotId.bot_id || null;
    q = filterOrBotId.q || null;
    status = filterOrBotId.status || null;
  } else {
    botId = filterOrBotId || null;
    q = qParam || null;
    status = statusParam || null;
  }
  if (MOCK) {
    await delay();
    const matches = (m) => {
      if (botId === 'shared' && m.bot_id !== null) return false;
      if (botId && botId !== 'all' && botId !== 'shared' && m.bot_id !== botId) return false;
      if (q && !m.text.toLowerCase().includes(q.toLowerCase())) return false;
      if (status && m.status !== status) return false;
      return true;
    };
    const proposed = mockMemoryProposed.filter(matches);
    const active = mockMemoryActive.filter(matches);
    return clone({ proposed, active, all: [...proposed, ...active] });
  }
  const params = new URLSearchParams();
  if (botId && botId !== 'all' && botId !== 'shared') params.set('bot_id', botId);
  if (status) params.set('status', status);
  if (q) params.set('q', q);
  const qs = params.toString() ? `?${params.toString()}` : '';
  let list = await request(`/memory${qs}`);
  if (botId === 'shared') list = list.filter((m) => m.bot_id === null);
  return {
    proposed: list.filter((m) => m.status === 'proposed'),
    active: list.filter((m) => m.status === 'active'),
    all: list,
  };
}

export async function createMemory(body) {
  if (MOCK) {
    await delay();
    if (!body || typeof body.text !== 'string' || !body.text.trim() || memoryTextTooLong(body.text)) mockFail(400, 'invalid', 'text');
    const entry = {
      id: `m${++mockMemorySeq}`,
      bot_id: body.bot_id || null,
      text: body.text.trim(),
      source: 'owner',
      status: 'active',
      version: 1,
      expires_at: body.expires_at || null,
      created_at: new Date().toISOString(),
    };
    mockMemoryActive.unshift(entry);
    return clone(entry);
  }
  return request('/memory', { method: 'POST', body });
}
export const addMemory = createMemory;

export async function patchMemory(id, body) {
  if (MOCK) {
    await delay();
    let entry = mockMemoryActive.find((m) => m.id === id) || mockMemoryProposed.find((m) => m.id === id);
    if (!entry) mockFail(404, 'not_found', 'memory entry not found');
    if (body.text !== undefined) {
      if (typeof body.text !== 'string' || !body.text.trim() || memoryTextTooLong(body.text)) mockFail(400, 'invalid', 'text');
      if (body.text !== entry.text) entry.version = (entry.version || 1) + 1;
      entry.text = body.text;
    }
    if (body.bot_id !== undefined) entry.bot_id = body.bot_id || null;
    if (body.expires_at !== undefined) entry.expires_at = body.expires_at || null;
    if (body.status !== undefined && body.status !== entry.status) {
      const oldStatus = entry.status;
      entry.status = body.status;
      if (oldStatus === 'proposed' && body.status === 'active') {
        const idx = mockMemoryProposed.findIndex((m) => m.id === id);
        if (idx >= 0) mockMemoryProposed.splice(idx, 1);
        mockMemoryActive.unshift(entry);
      } else if (body.status === 'archived') {
        const idxAct = mockMemoryActive.findIndex((m) => m.id === id);
        if (idxAct >= 0) mockMemoryActive.splice(idxAct, 1);
        const idxProp = mockMemoryProposed.findIndex((m) => m.id === id);
        if (idxProp >= 0) mockMemoryProposed.splice(idxProp, 1);
      }
    }
    return clone(entry);
  }
  return request(`/memory/${encodeURIComponent(id)}`, { method: 'PATCH', body });
}

export async function deleteMemory(id) {
  if (MOCK) {
    await delay();
    const idxAct = mockMemoryActive.findIndex((m) => m.id === id);
    if (idxAct >= 0) {
      mockMemoryActive.splice(idxAct, 1);
      return { ok: true };
    }
    const idxProp = mockMemoryProposed.findIndex((m) => m.id === id);
    if (idxProp >= 0) {
      mockMemoryProposed.splice(idxProp, 1);
      return { ok: true };
    }
    mockFail(404, 'not_found', 'memory entry not found');
  }
  return request(`/memory/${encodeURIComponent(id)}`, { method: 'DELETE' });
}

export async function decideMemory(id, status) {
  return patchMemory(id, { status });
}

// Вызовы пишутся в window.__usageCalls: {days}, значение так, как оно ушло бы в query-параметр.
export async function usageSummary(days = 7) {
  if (MOCK) {
    (window.__usageCalls = window.__usageCalls || []).push({ days });
    await delay(MOCK_USAGE_MODE === 'slow' ? 1200 : 120);
    if (MOCK_USAGE_MODE === 'fail' && !mockUsageListFailed) {
      mockUsageListFailed = true;
      mockFail(500, 'internal', 'internal');
    }
    if (MOCK_USAGE_MODE === 'none' || MOCK_USAGE_MODE === 'empty') {
      return buildMockUsage(days, true);
    }
    return clone(buildMockUsage(days));
  }
  const q = days ? `?days=${encodeURIComponent(days)}` : '';
  return request(`/usage/summary${q}`);
}

export async function listSchedules() {
  if (MOCK) { await delay(); return clone(mockSchedules); }
  return request('/schedules');
}
export async function patchSchedule(id, body) {
  if (MOCK) {
    await delay();
    const s = mockSchedules.find((x) => x.id === id);
    if (s) Object.assign(s, body);
    return clone(s);
  }
  return request(`/schedules/${id}`, { method: 'PATCH', body });
}
export async function runSchedule(id) {
  if (MOCK) { await delay(200); return { ok: true }; }
  return request(`/schedules/${id}/run`, { method: 'POST' });
}

// ---------------------------------------------------------------------------
// Лента активности и пауза ботов (docs/contracts.md §16)
// ---------------------------------------------------------------------------
// before: курсор из прошлого ответа (непрозрачная строка), kinds: массив видов, botId: фильтр по боту.
export async function listActivity({ botId, kinds, before, limit } = {}) {
  if (MOCK) {
    await delay(MOCK_ACTIVITY_MODE === 'slow' && !before ? 1500 : 150);
    if (MOCK_ACTIVITY_MODE === 'fail' && !mockActivityFailed) { mockActivityFailed = true; mockFail(500, 'internal', 'internal'); }
    if (MOCK_ACTIVITY_MODE === 'more-fail' && before && !mockActivityMoreFailed) { mockActivityMoreFailed = true; mockFail(500, 'internal', 'internal'); }
    const size = Math.min(Math.max(Number(limit) || 50, 1), 100);
    let rows = mockActivity.filter((item) => (!botId || item.bot_id === botId) && (!kinds || !kinds.length || kinds.includes(item.kind)));
    if (before) {
      let after;
      try { after = JSON.parse(atob(before)); } catch { mockFail(422, 'invalid', 'before'); }
      rows = rows.filter((item) => item.at < after[0] || (item.at === after[0] && item.id < after[1]));
    }
    const page = rows.slice(0, size);
    return clone({ items: page, next: rows.length > size ? mockCursor(page[page.length - 1]) : null });
  }
  const query = new URLSearchParams();
  if (botId) query.set('bot_id', botId);
  if (kinds && kinds.length) query.set('kind', kinds.join(','));
  if (before) query.set('before', before);
  if (limit) query.set('limit', String(limit));
  const text = query.toString();
  return request(`/activity${text ? `?${text}` : ''}`);
}

async function mockPauseGate() {
  await delay(150);
  if (MOCK_PAUSE_FAIL) mockFail(500, 'internal', 'internal');
}
export async function pauseBot(id, reason) {
  if (MOCK) {
    await mockPauseGate();
    const bot = mockBots.find((b) => b.id === id);
    if (!bot) mockFail(404, 'not_found');
    mockSetPaused(bot, true, reason);
    return mockPauseView(bot);
  }
  return request(`/bots/${encodeURIComponent(id)}/pause`, { method: 'POST', body: reason ? { reason } : {} });
}
export async function resumeBot(id) {
  if (MOCK) {
    await mockPauseGate();
    const bot = mockBots.find((b) => b.id === id);
    if (!bot) mockFail(404, 'not_found');
    mockSetPaused(bot, false);
    return mockPauseView(bot);
  }
  return request(`/bots/${encodeURIComponent(id)}/resume`, { method: 'POST', body: {} });
}
export async function pauseAll(reason) {
  if (MOCK) {
    await mockPauseGate();
    mockBots.forEach((bot) => mockSetPaused(bot, true, reason));
    return { bots: mockBots.map(mockPauseView) };
  }
  return request('/bots/pause-all', { method: 'POST', body: reason ? { reason } : {} });
}
export async function resumeAll() {
  if (MOCK) {
    await mockPauseGate();
    mockBots.forEach((bot) => mockSetPaused(bot, false));
    return { bots: mockBots.map(mockPauseView) };
  }
  return request('/bots/resume-all', { method: 'POST', body: {} });
}

// ---------------------------------------------------------------------------
// Процедуры (docs/contracts.md §14). Ошибки: ApiError с status, code (error) и detail; 501 not_implemented значит,
// что воспроизведение на сервере не включено.
// ---------------------------------------------------------------------------
const procPath = (id) => `/procedures/${encodeURIComponent(id)}`;

export async function listProcedures() {
  if (MOCK) {
    await mockProcGate(MOCK_PROC_MODE === 'slow' ? 1500 : 120);
    if (MOCK_PROC_MODE === 'fail' && !mockProcListFailed) { mockProcListFailed = true; mockFail(500, 'internal', 'internal'); }
    return mockProcs.map(mockProcView);
  }
  return request('/procedures');
}
export async function getProcedure(id) {
  if (MOCK) { await mockProcGate(); return mockProcView(mockProcFind(id)); }
  return request(procPath(id));
}
// POST /api/procedures/from-turn {thread_id, turn_id?, name} → процедура-черновик.
export async function createProcedureFromTurn(body) {
  if (MOCK) {
    await mockProcGate(250);
    mockProcCall('from-turn', null, body);
    if (!String(body.name || '').trim()) mockProc422('name', 'empty', 'is empty');
    const thread = Object.values(mockThreads).find((t) => t.id === body.thread_id);
    if (!thread) mockFail(404, 'not_found', 'thread not found');
    let events = (mockEvents[thread.id] || []).filter((e) => e.kind === 'browser_step');
    const turnId = body.turn_id || (events.length ? events[events.length - 1].turn_id : null);
    events = events.filter((e) => e.turn_id === turnId);
    const steps = mockProcStepsFromTurn(events);
    if (!steps.length) mockProc422('steps', 'empty', 'no recorded browser steps');
    if (mockProcNameTaken(body.name)) mockFail(409, 'conflict', 'процедура с таким названием уже есть');
    const p = {
      id: `pr${++mockProcSeq}`, bot_id: thread.bot_id, name: body.name.trim(), description: '', source: 'bot', status: 'draft', version: 1,
      created_at: new Date().toISOString(), updated_at: new Date().toISOString(), params: [], steps,
    };
    mockProcs.push(p);
    return mockProcView(p);
  }
  return request('/procedures/from-turn', { method: 'POST', body });
}
export async function patchProcedure(id, body) {
  if (MOCK) {
    await mockProcGate(150);
    mockProcCall('patch', id, body);
    const p = mockProcFind(id);
    mockProcValidate(body, p);
    if (body.name !== undefined && mockProcNameTaken(body.name, id)) mockFail(409, 'conflict', 'процедура с таким названием уже есть');
    if (body.status === 'active' && p.status === 'draft' && (body.steps || p.steps).some((s) => s.needs_value)) {
      mockProc422('status', 'steps_need_values', 'steps still need values');
    }
    const next = clone(p);
    for (const key of ['name', 'description', 'bot_id', 'status']) if (body[key] !== undefined) next[key] = body[key];
    if (body.params !== undefined) next.params = body.params;
    if (body.steps !== undefined) {
      // Ядро вычисляет риск само: шаг поднимается до вычисленного уровня, ниже него не опускается.
      next.steps = body.steps.map((s) => {
        const { computed_risk: _drop, ...rest } = s;
        return { ...rest, risk: mockStepRisk(rest) };
      });
    }
    if (body.params !== undefined || body.steps !== undefined) next.version = p.version + 1;
    next.updated_at = new Date().toISOString();
    Object.assign(p, next);
    return mockProcView(p);
  }
  return request(procPath(id), { method: 'PATCH', body });
}
export async function deleteProcedure(id) {
  if (MOCK) {
    await mockProcGate(150);
    mockProcCall('delete', id);
    const p = mockProcFind(id);
    if (mockProcRuns.some((r) => r.procedure_id === id && PROC_ACTIVE.includes(r.status))) mockFail(409, 'conflict', 'у процедуры есть активный запуск');
    mockProcs.splice(mockProcs.indexOf(p), 1);
    for (let i = mockProcRuns.length - 1; i >= 0; i--) if (mockProcRuns[i].procedure_id === id) mockProcRuns.splice(i, 1);
    return null;
  }
  return request(procPath(id), { method: 'DELETE' });
}
// GET /api/procedures/{id}/export → JSON-документ процедуры (формат задаёт ядро, клиент отдаёт его файлом как есть).
export async function exportProcedure(id) {
  if (MOCK) {
    await mockProcGate();
    mockProcCall('export', id);
    const p = mockProcFind(id);
    return {
      format: PROC_FORMAT, name: p.name, description: p.description, params: clone(p.params),
      steps: clone(p.steps),
    };
  }
  return request(`${procPath(id)}/export`);
}
// POST /api/procedures/import: принимает документ экспорта, создаёт черновик (source 'import').
export async function importProcedure(doc) {
  if (MOCK) {
    await mockProcGate(250);
    mockProcCall('import', null, doc);
    if (!doc || typeof doc !== 'object' || Array.isArray(doc)) mockProc422('body', 'type', 'must be an object');
    if (doc.format !== undefined && doc.format !== PROC_FORMAT) mockProc422('format', 'unsupported', 'unsupported format');
    if (typeof doc.name !== 'string' || !doc.name.trim()) mockProc422('name', 'empty', 'is empty');
    if (!Array.isArray(doc.steps) || !doc.steps.length) mockProc422('steps', 'empty', 'no steps');
    mockProcValidate({ params: doc.params || [], steps: doc.steps, name: doc.name }, { params: [], steps: [] }, { lenientRisk: true });
    if (mockProcNameTaken(doc.name)) mockFail(409, 'conflict', 'процедура с таким названием уже есть');
    const p = {
      id: `pr${++mockProcSeq}`, bot_id: null, name: doc.name.trim(), description: doc.description || '', source: 'import', status: 'draft', version: 1,
      created_at: new Date().toISOString(), updated_at: new Date().toISOString(), params: doc.params || [],
      steps: doc.steps.map((st) => { const { computed_risk: _drop, ...rest } = st; return { ...rest, risk: mockStepRisk(rest) }; }),
    };
    mockProcs.push(p);
    return mockProcView(p);
  }
  return request('/procedures/import', { method: 'POST', body: doc });
}
export async function listProcedureRuns(id) {
  if (MOCK) {
    await mockProcGate();
    mockProcFind(id);
    return mockProcRuns.filter((r) => r.procedure_id === id).sort((a, b) => b.created_at.localeCompare(a.created_at)).map(mockRunView);
  }
  return request(`${procPath(id)}/runs`);
}
// POST /api/procedures/{id}/run {bot_id, thread_id?, params} → procedure_run. 501 not_implemented: воспроизведение выключено.
export async function runProcedure(id, body) {
  if (MOCK) {
    await mockProcGate(250);
    mockProcCall('run', id, body);
    const p = mockProcFind(id);
    if (MOCK_REPLAY_OFF) mockFail(501, 'not_implemented', 'procedure replay is not enabled');
    if (p.status !== 'active') mockFail(409, 'conflict', p.status === 'draft' ? 'процедура ещё черновик' : 'процедура в архиве');
    const botId = body.bot_id || p.bot_id;
    const bot = mockBots.find((b) => b.id === botId);
    if (!bot) mockFail(400, 'invalid', 'bot_id: бот не найден');
    const values = body.params || {};
    for (const prm of p.params) {
      const v = values[prm.name];
      const empty = v === undefined || v === null || v === '';
      if (prm.required && empty && (prm.default == null || prm.default === '')) mockFail(400, 'invalid', `params.${prm.name}: обязательный параметр`);
      if (prm.secret && !empty) {
        if (!/^vault:/.test(String(v))) mockFail(400, 'invalid', `params.${prm.name}: секретный параметр принимает только ссылку vault:имя`);
        if (!mockSecrets.some((s) => `vault:${s.name}` === v)) mockFail(400, 'invalid', `params.${prm.name}: секрет не найден`);
      }
    }
    for (const key of Object.keys(values)) if (!p.params.some((x) => x.name === key)) mockFail(400, 'invalid', `params.${key}: параметр не описан`);
    const thread = body.thread_id || (mockThreads[bot.id] && mockThreads[bot.id].id) || `t-${bot.id}`;
    const run = {
      id: `run${++mockProcSeq}`, procedure_id: id, procedure_version: p.version, bot_id: bot.id, thread_id: thread, turn_id: `tu-run${mockProcSeq}`, status: 'queued',
      params: clone(values), steps: clone(p.steps), next_step: 0, step_log: [], error: null, started_at: null, finished_at: null, created_at: new Date().toISOString(),
    };
    mockProcRuns.push(run);
    return mockRunView(run);
  }
  return request(`${procPath(id)}/run`, { method: 'POST', body });
}
export async function getProcedureRun(id) {
  if (MOCK) {
    await mockProcGate(60);
    const run = mockProcRuns.find((r) => r.id === id);
    if (!run) mockFail(404, 'not_found', 'run not found');
    return mockRunView(run);
  }
  return request(`/procedure-runs/${encodeURIComponent(id)}`);
}
export async function stopProcedureRun(id) {
  if (MOCK) {
    await mockProcGate(150);
    mockProcCall('stop', id);
    const run = mockProcRuns.find((r) => r.id === id);
    if (!run) mockFail(404, 'not_found', 'run not found');
    if (!PROC_ACTIVE.includes(run.status)) mockFail(409, 'conflict', 'запуск уже завершён');
    run.status = 'stopped';
    run.finished_at = new Date().toISOString();
    return mockRunView(run);
  }
  return request(`/procedure-runs/${encodeURIComponent(id)}/stop`, { method: 'POST' });
}
// POST /api/procedure-runs/{id}/decide {action: retry|skip|stop}: решение владельца, когда запуск ждёт человека (waiting_human).
// Ядро пока отвечает 501 not_implemented; мок решает сам, если воспроизведение включено.
export async function decideProcedureRun(id, action) {
  if (MOCK) {
    await mockProcGate(150);
    mockProcCall('decide', id, { action });
    const run = mockProcRuns.find((r) => r.id === id);
    if (!run) mockFail(404, 'not_found', 'run not found');
    if (!['retry', 'skip', 'stop'].includes(action)) mockFail(400, 'invalid', 'action');
    if (MOCK_REPLAY_OFF) mockFail(501, 'not_implemented', 'not_implemented');
    if (run.status !== 'waiting_human') mockFail(409, 'conflict', 'run is not waiting for a decision');
    const nowIso = new Date().toISOString();
    const snapshot = run.steps || [];
    if (action === 'stop') { run.status = 'stopped'; run.finished_at = nowIso; } else if (action === 'skip') {
      const step = snapshot[run.next_step];
      if (step) run.step_log.push({ step_id: step.id, status: 'skipped', at: nowIso, duration_ms: 0 });
      run.next_step += 1;
      if (run.next_step >= snapshot.length) { run.status = 'done'; run.finished_at = nowIso; } else run.status = 'running';
    } else run.status = 'running';
    return mockRunView(run);
  }
  return request(`/procedure-runs/${encodeURIComponent(id)}/decide`, { method: 'POST', body: { action } });
}
// GET /api/secrets → [{name, bot_id}]: только имена, значений нет. null: список недоступен, имя вводится вручную.
export async function listSecrets() {
  if (MOCK) {
    await mockProcGate(60);
    return MOCK_SECRETS_OFF ? null : clone(mockSecrets);
  }
  try { return await request('/secrets'); } catch (err) {
    if ([404, 405, 501].includes(err.status)) return null;
    throw err;
  }
}

export async function macStatus() {
  if (MOCK) { await delay(); return clone(mockMac); }
  return request('/mac/status');
}

export async function pushSubscribe(sub, device) {
  if (MOCK) return { ok: true };
  return request('/push/subscribe', { method: 'POST', body: { ...sub, device } });
}

export function botById(id) {
  return mockBots.find((b) => b.id === id);
}
export { mockBots };
