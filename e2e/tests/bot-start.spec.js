import { expect, test } from '@playwright/test';

// Создание бота (docs/contracts.md §9, «Запуск бота после ответа»): ядро отвечает сразу со status starting, контейнер
// поднимается фоном. Мок: ?create=starting (запуск 3 с), ?create=lost (бот создан, ответ потерян), ?create=down (ответа нет, бота нет).
const DESC = 'Каждый день проверяй новые вакансии SRE и присылай краткий список';

async function create(page, mode) {
  await page.goto(`/?mock=1&create=${mode}#/bots/new?d=${encodeURIComponent(DESC)}`);
  await page.getByRole('button', { name: 'Собрать' }).click();
  await expect(page.getByLabel('Имя')).toBeVisible({ timeout: 5000 });
  await page.getByRole('button', { name: 'Создать', exact: true }).click();
}

test.describe('бот в starting', () => {
  test('создание открывает тред: баннер «Бот запускается», подсказка под вводом, потом они исчезают сами', async ({ page }) => {
    await create(page, 'starting');
    await expect(page).toHaveURL(/#\/threads\/t-draft-/);
    const banner = page.locator('[data-bot-state="starting"]');
    await expect(banner).toBeVisible();
    await expect(banner).toContainText('Бот запускается');
    await expect(page.locator('[data-bot-hint-slot]')).toContainText('сообщение подождёт запуска');
    await expect(page.locator('#composer-input')).toBeEnabled(); // ввод открыт: сообщение встаёт в очередь
    // мок переводит бота в idle через 3 с, экран перечитывает список каждые 2 с и обновляется на месте
    await expect(banner).toHaveCount(0, { timeout: 10_000 });
    await expect(page.locator('[data-bot-hint-slot]')).toBeEmpty();
  });

  test('в списке бот показан как «Запускается», затем «Готово»', async ({ page, isMobile }) => {
    test.skip(!isMobile, 'карточки .bot-card только в мобильной раскладке; на Mac список в боковой панели');
    await create(page, 'starting');
    await expect(page).toHaveURL(/#\/threads\/t-draft-/);
    await page.evaluate(() => { location.hash = '#/'; }); // без перезагрузки: мок хранит созданного бота в памяти
    const card = page.locator('.bot-card[data-bot-status="starting"]');
    await expect(card.locator('.status-line')).toHaveText('Запускается');
    await expect(page.locator('.bot-card[data-bot-status="starting"]')).toHaveCount(0, { timeout: 10_000 });
    await expect(page.locator('.bot-card[data-bot-status="idle"] .status-line', { hasText: 'Готово' }).first()).toBeVisible();
  });

  test('ответ на создание потерян, а бот создан: экран находит его по списку и открывает тред, ошибки нет', async ({ page }) => {
    await create(page, 'lost');
    await expect(page).toHaveURL(/#\/threads\/t-draft-/);
    await expect(page.locator('#bn-create-error')).toBeHidden();
  });

  test('ответа нет и бота нет: ошибка и кнопка «Повторить»', async ({ page }) => {
    await create(page, 'down');
    await expect(page.locator('#bn-create-error')).toContainText('бот не создан');
    await expect(page.getByRole('button', { name: 'Повторить' })).toBeEnabled();
    await expect(page).not.toHaveURL(/#\/threads\//);
  });
});

test.describe('поиск созданного бота после потерянного ответа', () => {
  test('сетевая ошибка, 502 и 504 ищут бота по имени и времени; прочие отказы список не читают', async ({ page }) => {
    await page.goto('/?mock=1');
    const out = await page.evaluate(async () => {
      const api = await import('/api.js');
      const now = Date.now();
      const body = { name: 'Скаут' };
      const bot = (name, at, id) => ({ id, name, created_at: new Date(at).toISOString(), status: 'starting' });
      const lost = (err) => api.createBotRecovering(async () => { throw err; }, async () => [bot('Скаут', now + 500, 'made')], body, () => now);
      const refused = async (status) => {
        let listed = false;
        try {
          await api.createBotRecovering(async () => { throw new api.ApiError(status, { error: 'x' }); }, async () => { listed = true; return []; }, body, () => now);
        } catch (err) { return { status: err.status, listed }; }
        return null;
      };
      return {
        network: (await lost(new TypeError('Failed to fetch'))).id,
        gateway: (await lost(new api.ApiError(502, null))).id,
        timeout: (await lost(new api.ApiError(504, null))).id,
        refused: [await refused(400), await refused(409), await refused(500), await refused(503)],
        old: api.findCreatedBot([bot('Скаут', now - 600_000, 'old')], body, now),
        other: api.findCreatedBot([bot('Другой', now + 1, 'x')], body, now),
        newest: api.findCreatedBot([bot('Скаут', now + 10, 'a'), bot('Скаут', now + 20, 'b')], body, now).id,
      };
    });
    expect(out.network).toBe('made');
    expect(out.gateway).toBe('made');
    expect(out.timeout).toBe('made');
    expect(out.refused).toEqual([400, 409, 500, 503].map((status) => ({ status, listed: false })));
    expect(out.old).toBeNull();
    expect(out.other).toBeNull();
    expect(out.newest).toBe('b');
  });
});
