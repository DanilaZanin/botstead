// registry.js: чистые помощники по реестру провайдеров и моделей (docs/contracts.md §11–12) без разметки и сети.
// Ими пользуются экраны провайдеров, мастер бота, приветствие и карточки ботов.
import { esc, fmtAgo, plural } from './ui.js';

export const CLI_LABEL = { claude: 'Claude Code', codex: 'Codex', agy: 'Antigravity' };
export const CLI_NOTE = { claude: 'подписка Claude', codex: 'подписка ChatGPT', agy: 'аккаунт Google' };
export const VENDORS = [
  { kind: 'anthropic_api', label: 'Anthropic', name: 'Anthropic API', placeholder: 'sk-ant-…', keyHint: 'console.anthropic.com в разделе API Keys' },
  { kind: 'openai_api', label: 'OpenAI', name: 'OpenAI API', placeholder: 'sk-…', keyHint: 'platform.openai.com в разделе API keys' },
  { kind: 'google_api', label: 'Google', name: 'Google API', placeholder: 'AIza…', keyHint: 'aistudio.google.com в разделе Get API key' },
];
const RUNNER = { anthropic_api: 'claude', openai_api: 'codex', openai_compatible: 'codex', google_api: 'gemini' };

// Какой раннер (claude, codex, gemini) отвечает за провайдера: от него зависит цвет аватара и значка.
export function runnerKind(provider) {
  if (!provider) return 'claude';
  if (provider.kind === 'cli_subscription') return provider.cli === 'agy' ? 'gemini' : provider.cli;
  return RUNNER[provider.kind] || 'claude';
}

export function kindLabel(provider) {
  if (provider.kind === 'cli_subscription') return 'Подписка';
  if (provider.kind === 'openai_compatible') return 'Свой адрес';
  return 'API-ключ';
}

// Состояние ключа (docs/contracts.md §11): secret_tail это последние 4 символа ключа или null, если ключ короче 12 символов
// или сохранён до миграции 015. Полный ключ API не отдаёт.
export function keyTail(p) {
  return typeof p.secret_tail === 'string' ? p.secret_tail.trim().slice(-4) : '';
}
export function keyText(p, { capital = false } = {}) {
  if (!p.has_secret) return capital ? 'Без ключа' : 'без ключа';
  const tail = keyTail(p);
  if (tail) return `•••• ${tail}`;
  return capital ? 'Ключ сохранён' : 'ключ сохранён';
}
// То же для разметки: точки читаются вслух как «ключ, последние символы …».
export function keyHtml(p, opts) {
  const text = keyText(p, opts);
  const tail = p.has_secret ? keyTail(p) : '';
  return tail ? `<span role="img" aria-label="ключ, последние символы ${esc(tail)}">${esc(text)}</span>` : esc(text);
}

export function hostPath(url) {
  try {
    const u = new URL(url);
    return `${u.host}${u.pathname.replace(/\/$/, '')}`;
  } catch { return String(url || ''); }
}

// Ядро кладёт в last_error текст исключения. Разбираем его по смыслу: по тексту нельзя отличить 401 от 404,
// поэтому «provider check failed» читается как отказ ключа или пути, а подсказка называет оба.
export function providerProblem(p) {
  const e = String(p.last_error || '');
  if (p.kind === 'cli_subscription') {
    if (/launcher unavailable/i.test(e)) return { code: 'launcher', short: 'Проверка недоступна', title: 'Сервер входа недоступен', text: 'Проверить подписку сейчас не получилось. Попробуйте позже.' };
    return { code: 'login', short: 'Нужен вход', title: 'Нужен вход', text: 'Войдите в аккаунт подписки: кнопка «Войти».' };
  }
  const compat = p.kind === 'openai_compatible';
  if (/non-public|HTTP requires|HTTP is allowed|origin without|invalid provider|resolves to/i.test(e)) {
    return { code: 'private', short: 'Адрес закрыт', title: 'Адрес закрыт', text: 'Серверу нельзя ходить на этот адрес: он в локальной сети или не проходит проверку. Исключения задаёт администратор сервера.' };
  }
  if (/did not resolve/i.test(e)) return { code: 'down', short: 'Адрес не найден', title: 'Адрес не найден', text: 'Имя хоста не разрешается в адрес. Проверьте написание.' };
  if (/provider check failed/i.test(e)) {
    if (compat) return { code: 'key', short: 'Адрес вернул ошибку', title: 'Адрес вернул ошибку', text: 'Ключ не принят, либо путь не тот: обычно адрес заканчивается на /v1.' };
    const vendor = VENDORS.find((v) => v.kind === p.kind);
    return { code: 'key', short: 'Ключ отклонён', title: 'Ключ отклонён', text: `Провайдер не принял ключ.${vendor ? ` Новый ключ создаётся на ${vendor.keyHint}.` : ''}` };
  }
  if (/no models|expecting|json|decode|too large/i.test(e)) {
    return { code: 'bad', short: compat ? 'Не совместим с OpenAI' : 'Нет списка моделей', title: compat ? 'Адрес не совместим с OpenAI' : 'Провайдер не вернул модели', text: 'Адрес ответил, но списка моделей в нужном формате не отдал. Обычно адрес заканчивается на /v1.' };
  }
  return { code: 'down', short: e ? 'Не отвечает' : 'Проверка не прошла', title: e ? 'Адрес не отвечает' : 'Проверка не прошла', text: 'Сервер моделей должен быть запущен и доступен с сервера botstead, а не только с этого устройства.' };
}

// Адрес изменился после одобрения: ядро ставит pending_admin и пишет в last_error «адрес изменился, нужно повторное одобрение».
export function isReapproval(p) {
  return p.status === 'pending_admin' && /изменил|повторн/i.test(String(p.last_error || ''));
}

// Состояния, в которых провайдер не запускается, но это не ошибка проверки: ждёт администратора или сохранён без проверки (§11).
export function pendingProblem(p) {
  if (p.status === 'pending_admin') {
    if (isReapproval(p)) {
      return { code: 'reapproval', short: 'Нужно повторное одобрение', title: 'Адрес сервера изменился, нужно повторное одобрение', text: 'Администратор уже разрешал этот адрес, но имя сервера теперь указывает на другие IP-адреса. Пока администратор не разрешит их заново, боты на моделях провайдера не отвечают.' };
    }
    return { code: 'pending', short: 'Ждёт одобрения администратора', title: 'Ждёт одобрения администратора', text: 'Адрес находится во внутренней сети. Администратор должен разрешить его вручную. Пока этого нет, боты на моделях провайдера не отвечают.' };
  }
  if (p.status === 'unchecked') {
    return { code: 'unchecked', short: 'Не проверен', title: 'Сохранён без проверки', text: 'Ключ сохранён, но проверить его не получилось. Боты не смогут отвечать, пока проверка не пройдёт.' };
  }
  return null;
}

// Строка статуса провайдера: цвет точки (success, danger, attention, neutral) и текст.
export function providerStatus(p, { ago = true } = {}) {
  const cli = p.kind === 'cli_subscription';
  if (p.status === 'ok') return { kind: 'success', text: cli ? 'Вход выполнен' : `Работает${ago && p.last_check_at ? ` · проверен ${fmtAgo(p.last_check_at)}` : ''}` };
  if (p.status === 'error') {
    const problem = providerProblem(p);
    return { kind: problem.code === 'login' ? 'attention' : 'danger', text: problem.short, problem };
  }
  if (p.status === 'disabled') return { kind: 'neutral', text: 'Отключён' };
  const pending = pendingProblem(p);
  if (pending) return { kind: 'attention', text: pending.short, problem: pending };
  return cli ? { kind: 'attention', text: 'Нужен вход' } : { kind: 'neutral', text: 'Не проверен' };
}

// Модель можно привязать к боту, если провайдер в порядке. Google API ядро пока привязывать не умеет (§11).
export function providerUsable(p) {
  return p.status === 'ok' && p.kind !== 'google_api';
}
export function unusableReason(p) {
  if (p.kind === 'google_api' && p.status === 'ok') return 'Google API пока нельзя привязать к боту';
  return providerStatus(p).text;
}

// Группы для выбора модели: по провайдерам, в каждой включённые модели. Недоступные остаются видимыми, с причиной.
export function modelGroups(providers, models) {
  return providers
    .map((provider) => ({
      provider,
      models: models
        .filter((m) => m.provider_id === provider.id && m.enabled)
        .map((model) => ({ model, usable: providerUsable(provider), reason: providerUsable(provider) ? '' : unusableReason(provider) })),
    }))
    .filter((g) => g.models.length);
}
export function usableModels(groups) {
  return groups.flatMap((g) => g.models.filter((m) => m.usable).map((m) => ({ provider: g.provider, model: m.model })));
}
export function usableCount(providers, models) {
  return usableModels(modelGroups(providers, models)).length;
}

export function modelCountText(enabled, total) {
  if (!total) return 'моделей нет';
  if (!enabled) return 'модели выключены';
  return `${enabled} ${plural(enabled, 'модель', 'модели', 'моделей')}`;
}

// Статус бота без модели: в ядре его не снимает привязка, поэтому «нет модели» только пока provider_id или model_id пусты.
export function botNoModel(bot) {
  return bot.status === 'no_model' && !(bot.provider_id && bot.model_id);
}
