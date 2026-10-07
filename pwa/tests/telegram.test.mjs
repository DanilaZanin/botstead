import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
globalThis.location = { search: '?mock=1' };
globalThis.window = {};
const { parseTelegramChatIds, telegramChannelMarkup, renderTelegramState } = await import('../telegram.js');

test('chat IDs accept exact signed integers and clearing the list', () => {
  assert.deepEqual(parseTelegramChatIds('123, -100456'), [123, -100456]);
  assert.deepEqual(parseTelegramChatIds('  '), []);
  for (const bad of ['1abc', '1,,2', '1.5', '9007199254740993', '+2']) {
    assert.throws(() => parseTelegramChatIds(bad));
  }
});

test('channel markup escapes stored values and never renders a token', () => {
  const html = telegramChannelMarkup({ id: 'x', enabled: true, allowed_chat_ids: [1], token_last4: '<bad>', webhook_hint: '<script>alert(1)</script>', token: 'secret-canary' });
  assert.match(html, /&lt;bad&gt;/);
  assert.match(html, /&lt;script&gt;/);
  assert.doesNotMatch(html, /<script>|secret-canary/);
  assert.match(html, /value="1"/);
});

test('state renderer loads channel and shows a safe error', async () => {
  const state = { innerHTML: '', isConnected: true };
  const scope = { querySelector: () => state };
  await renderTelegramState(scope, 'bot', async (id) => ({ id, enabled: true, allowed_chat_ids: [42], token_last4: 'last' }));
  assert.match(state.innerHTML, /value="42"/);
  await renderTelegramState(scope, 'bot', async () => { throw new Error('secret-canary'); });
  assert.match(state.innerHTML, /Не удалось загрузить/);
  assert.doesNotMatch(state.innerHTML, /secret-canary/);
});

test('schedule action has one branch that still patches the schedule', async () => {
  const source = await readFile(new URL('../app.js', import.meta.url), 'utf8');
  assert.equal((source.match(/action === 'toggle-schedule'/g) || []).length, 1);
  assert.match(source, /action === 'toggle-schedule'\)[\s\S]*?api\.patchSchedule\(id, \{ enabled:/);
});

test('mock API persists channel without returning the raw token', async () => {
  globalThis.location = { search: '?mock=1&runs=manual' };
  globalThis.window = {};
  const api = await import('../api.js');
  const token = '123456:secret-canary';
  const saved = await api.putTelegramChannel('test-bot', { token, allowed_chat_ids: [42], enabled: true });
  assert.equal(saved.token_last4, 'nary');
  assert.doesNotMatch(JSON.stringify(saved), /secret-canary/);
  assert.deepEqual((await api.getTelegramChannel('test-bot')).allowed_chat_ids, [42]);
  await api.deleteTelegramChannel('test-bot');
  assert.equal((await api.getTelegramChannel('test-bot')).enabled, false);
});
