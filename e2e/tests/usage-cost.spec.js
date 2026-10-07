import { expect, test } from '@playwright/test';

// Оценка стоимости на экране «Расход»: «≈ $X,XX» у моделей и ботов, общая строка
// «Оценка стоимости: ≈ $X,XX» с пометкой «частично: не у всех моделей есть цена».
// Контракт: GET /api/usage/summary возвращает cost_usd в каждой строке модели/бота,
// cost_usd_total и cost_partial на верхнем уровне. Подписочные модели (нет цены) → null → ничего не рисуем.
// Мок-режим: ?mock=1.

const YOU = /(^|[^а-яё])(ты|тебя|тебе|тобой|твой|твоя|твоё|твои|твоих)(?![а-яё])|(Опиши|Выбери|Попробуй|Открой|Нажми|Введи|Смотри)(?![а-яё])/i;

async function openUsage(page, query = '') {
  await page.goto(`/?mock=1${query}#/usage`);
  await expect(page.getByRole('heading', { name: 'Расход', level: 1 })).toBeVisible({ timeout: 5000 });
}

test.describe('оценка стоимости на экране «Расход»', () => {
  test('в карточке периода есть строка «Оценка стоимости» с суммой «≈ $X,XX»', async ({ page }) => {
    await openUsage(page);

    // Заголовок карточки: «Всего за период (за 7 дней)» + стоимость.
    const chartCard = page.locator('.usage-chart-card');
    await expect(chartCard).toBeVisible();

    // Стоимость по умолчанию в моке: claude-sonnet-5 (11,655) + gpt-5.4 (3,0625) + gemini-3.1-pro-preview (0,6125) = 15,33.
    // Округляем до 2 знаков.
    await expect(chartCard).toContainText('Оценка стоимости:');
    const costText = await chartCard.locator('.t-callout').filter({ hasText: /\$/ }).first().innerText();
    expect(costText).toMatch(/≈\s*\$15,33/);

    // Пометка «частично» видна (claude-opus-5-5 без цены — подписка).
    await expect(chartCard).toContainText('частично: не у всех моделей есть цена');
  });

  test('у моделей с ценой есть ячейка «≈ $X,XX», у подписки ячейка пустая', async ({ page }) => {
    await openUsage(page);

    const table = page.locator('.usage-table');
    await expect(table).toBeVisible();

    // Колонка «Стоимость» появилась.
    const headers = table.locator('th');
    await expect(headers).toHaveText([
      'Модель', 'Вход', 'Выход', 'Кэш чтение', 'Кэш запись', 'Итого', 'Ходы', 'Стоимость',
    ]);

    // claude-sonnet-5: 11,655 → «≈ $11,66»
    const sonnetRow = table.locator('tbody tr').filter({ hasText: 'claude-sonnet-5' });
    await expect(sonnetRow).toBeVisible();
    const sonnetCost = await sonnetRow.locator('td').last().innerText();
    expect(sonnetCost).toMatch(/≈\s*\$11,66/);

    // gpt-5.4: 3,0625 → «≈ $3,06»
    const gptRow = table.locator('tbody tr').filter({ hasText: 'gpt-5.4' });
    await expect(gptRow).toBeVisible();
    const gptCost = await gptRow.locator('td').last().innerText();
    expect(gptCost).toMatch(/≈\s*\$3,06/);

    // gemini-3.1-pro-preview: 0,6125 → «≈ $0,61»
    const geminiRow = table.locator('tbody tr').filter({ hasText: 'gemini-3.1-pro-preview' });
    await expect(geminiRow).toBeVisible();
    const geminiCost = await geminiRow.locator('td').last().innerText();
    expect(geminiCost).toMatch(/≈\s*\$0,61/);

    // claude-opus-5-5: подписочная модель, цены нет → ячейка «Стоимость» пустая.
    const opusRow = table.locator('tbody tr').filter({ hasText: 'claude-opus-5-5' });
    await expect(opusRow).toBeVisible();
    const opusCost = await opusRow.locator('td').last().innerText();
    expect(opusCost.trim()).toBe('');
  });

  test('у ботов в строке «за период: …» дописывается «· ≈ $X,XX» при наличии цены', async ({ page }) => {
    await openUsage(page);

    // sre использует claude-opus-5-5 (подписка) → цены нет → метка «· ≈ …» отсутствует.
    const sreRow = page.locator('.usage-bot-row', { hasText: 'SRE' });
    await expect(sreRow).toBeVisible();
    const sreMeta = await sreRow.locator('.usage-bot-meta').last().innerText();
    expect(sreMeta).not.toContain('≈');

    // coder использует gpt-5.4 → есть цена.
    const coderRow = page.locator('.usage-bot-row', { hasText: 'Кодер' });
    await expect(coderRow).toBeVisible();
    const coderMeta = await coderRow.locator('.usage-bot-meta').last().innerText();
    expect(coderMeta).toContain('≈');
    expect(coderMeta).toMatch(/≈\s*\$3,06/);
  });

  test('общая стоимость есть, пометка «частично» скрыта, если у всех моделей есть цена', async ({ page }) => {
    // usage=full-cost убирает подписочную claude-opus-5-5 из моделей и переключает SRE на sonnet,
    // так что у всех ботов и моделей известная цена → cost_partial=false.
    await openUsage(page, '&usage=full-cost');

    const chartCard = page.locator('.usage-chart-card');
    await expect(chartCard).toContainText('Оценка стоимости:');
    const totalText = await chartCard.locator('.t-callout').filter({ hasText: /\$/ }).first().innerText();
    expect(totalText).toMatch(/≈\s*\$\d+,\d{2}/);
    await expect(chartCard).not.toContainText('частично: не у всех моделей есть цена');

    // Колонка «Стоимость» заполнена во всех видимых строках моделей.
    const rows = page.locator('.usage-table tbody tr');
    const count = await rows.count();
    for (let i = 0; i < count; i++) {
      const row = rows.nth(i);
      const cost = await row.locator('td').last().innerText();
      expect(cost).toMatch(/≈\s*\$/);
    }
  });

  test('пустое состояние: нет блока стоимости и нет ошибок', async ({ page }) => {
    await openUsage(page, '&usage=none');

    // Никаких заголовков про стоимость быть не должно.
    await expect(page.getByText('Расхода пока нет')).toBeVisible();
    expect(await page.getByText('Оценка стоимости:').count()).toBe(0);
    expect(await page.getByText('частично: не у всех моделей есть цена').count()).toBe(0);
    await expect(page.locator('.usage-chart-card')).toHaveCount(0);
    await expect(page.locator('.usage-table')).toHaveCount(0);
  });

  test('английский интерфейс: «Estimated cost:», «Cost», «partial: …»', async ({ page }) => {
    await page.addInitScript((code) => {
      try { localStorage.setItem('bothub.lang', code); } catch { /* приватный режим */ }
    }, 'en');
    await page.goto('/?mock=1&i18n=debug#/usage');
    await expect(page.getByRole('heading', { name: 'Usage', level: 1 })).toBeVisible();

    // Переводы стоимости присутствуют.
    await expect(page.getByText('Estimated cost:')).toBeVisible();
    await expect(page.getByText('partial: not all models have a price')).toBeVisible();
    await expect(page.locator('.usage-table th').filter({ hasText: 'Cost' })).toBeVisible();

    // В отчёте о пропусках не должно быть наших ключей.
    const missing = await page.evaluate(() => window.__i18nMissing || []);
    expect(missing.some((m) => m.startsWith('Оценка стоимости'))).toBe(false);
    expect(missing.some((m) => m === 'Стоимость')).toBe(false);
    expect(missing.some((m) => m.startsWith('частично'))).toBe(false);
  });

  test('на экране нет обращений на «ты» и нет длинного тире', async ({ page }) => {
    await openUsage(page);

    const text = await page.locator('#app').innerText();
    expect(text.match(YOU)?.[0]).toBeUndefined();
    expect(text).not.toContain('—');
  });
});
