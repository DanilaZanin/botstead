import { expect, test } from '@playwright/test';

// Офлайн-сценарий: нужен настоящий service worker и сервер с обычными заголовками кэширования
// (второй webServer в playwright.config.js, порт 4174, STATIC_CACHE=1). Основной сервер отдаёт no-store.
// Мок не используем: он отвечает из памяти и без сети.
test.use({ baseURL: 'http://127.0.0.1:4174', serviceWorkers: 'allow' });

async function waitForShellCache(page) {
  await page.evaluate(() => navigator.serviceWorker.ready);
  await expect.poll(() => page.evaluate(() => Boolean(navigator.serviceWorker.controller))).toBe(true);
  await expect.poll(() => page.evaluate(async () => {
    const shell = (await caches.keys()).find((key) => key.startsWith('bothub-shell'));
    return shell ? (await (await caches.open(shell)).keys()).length : 0;
  })).toBeGreaterThanOrEqual(10);
}

test('офлайн: оболочка открывается, видно «Нет связи» и «Повторить», чужие данные из кэша не показываются', async ({ page, context }) => {
  await page.goto('/');
  await waitForShellCache(page);

  // остатки прошлого пользователя: кэш данных, заметки, признак пользователя
  await page.evaluate(async () => {
    localStorage.setItem('bothub_uid', 'u-other');
    localStorage.setItem('bothub_invite_notes', JSON.stringify({ x: 'Заметка чужого пользователя' }));
    const cache = await caches.open('bothub-data-u-other');
    await cache.put('/api/bots', new Response(JSON.stringify([{ id: 'x', name: 'ЧужойБот' }]), { headers: { 'content-type': 'application/json' } }));
  });

  await context.setOffline(true);
  await page.reload();

  await expect(page.getByRole('heading', { name: 'Нет связи' })).toBeVisible();
  const retry = page.getByRole('button', { name: 'Повторить' });
  await expect(retry).toBeVisible();
  const box = await retry.boundingBox();
  expect(box.height).toBeGreaterThanOrEqual(44);
  for (const foreign of ['ЧужойБот', 'Заметка чужого пользователя', 'Скаут', 'u-other']) {
    await expect(page.getByText(foreign)).toHaveCount(0);
  }

  // пока связи нет, «Повторить» остаётся на том же экране
  await retry.click();
  await expect(page.getByRole('heading', { name: 'Нет связи' })).toBeVisible();

  // связь вернулась: «Повторить» ведёт дальше, экран «Нет связи» уходит
  await context.setOffline(false);
  await page.getByRole('button', { name: 'Повторить' }).click();
  await expect(page.getByRole('heading', { name: 'Нет связи' })).toHaveCount(0);
  await expect(page.getByText('ЧужойБот')).toHaveCount(0);
});

test('оболочка открывается офлайн и по адресу с query из манифеста (?utm_source=pwa)', async ({ page, context }) => {
  await page.goto('/');
  await waitForShellCache(page);
  await context.setOffline(true);
  await page.goto('/?utm_source=pwa');
  await expect(page.getByRole('heading', { name: 'Нет связи' })).toBeVisible();
});
