import { esc } from './ui.js';

export function parseTelegramChatIds(value) {
  if (!value.trim()) return [];
  return value.split(',').map((item) => {
    const part = item.trim();
    if (!/^-?\d+$/.test(part)) throw new Error('Укажите целые Chat ID через запятую');
    const id = Number(part);
    if (!Number.isSafeInteger(id)) throw new Error('Chat ID выходит за допустимый диапазон');
    return id;
  });
}

export function telegramChannelMarkup(channel) {
  const hasChannel = Boolean(channel?.id);
  const ids = Array.isArray(channel?.allowed_chat_ids) ? channel.allowed_chat_ids : [];
  return `<div class="stack gap-2">
    <span class="t-footnote">${hasChannel ? `Последние 4 символа токена: ${esc(channel.token_last4 || '')}` : 'Канал Telegram не настроен'}</span>
    <label for="tg-token">Токен бота Telegram</label>
    <input id="tg-token" class="input" type="password" autocomplete="off" placeholder="${hasChannel ? 'Оставьте пустым, чтобы сохранить токен' : 'Введите токен бота Telegram'}">
    <label for="tg-chat-ids">Chat ID через запятую</label>
    <input id="tg-chat-ids" class="input" inputmode="numeric" value="${esc(ids.join(', '))}">
    <label class="switch-row" for="tg-enabled"><span>Включён</span><input id="tg-enabled" type="checkbox" ${channel?.enabled ? 'checked' : ''}></label>
    ${channel?.webhook_hint ? `<span class="t-footnote">${esc(channel.webhook_hint)}</span>` : ''}
    <button type="button" class="btn btn-primary" data-action="save-telegram-token">Сохранить</button>
    ${hasChannel ? '<button type="button" class="btn btn-danger" data-action="delete-telegram-channel">Удалить канал</button>' : ''}
  </div>`;
}

export async function renderTelegramState(scope, botId, load) {
  const state = scope?.querySelector('[data-telegram-state]');
  if (!state) return;
  try {
    const channel = await load(botId);
    if (state.isConnected === false) return;
    state.innerHTML = telegramChannelMarkup(channel);
  } catch {
    if (state.isConnected === false) return;
    state.innerHTML = '<span role="alert">Не удалось загрузить настройки Telegram</span>';
  }
}

export function mountTelegram(scope, botId, load) {
  void renderTelegramState(scope, botId, load);
}
