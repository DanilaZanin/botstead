import { expect, test } from '@playwright/test';

// Боковая панель треда (десктоп), раздел «Квоты»: боту на CLI-подписке показываем только его метр
// (Claude/Codex/Gemini по runner-провайдеру), а боту на API-ключе или OpenAI-совместимом сервере
// (OpenRouter) — расход за сегодня из той же сводки, что грузит экран расхода; чужие метры не показываем.
// Мок-режим: ?mock=1. Привязки мока: Мак → p-claude (подписка), Скаут → p-agy (подписка gemini),
// Кодер → p-openai (API-ключ), Архив → p-ollama (свой адрес). &usage=none — сводка расхода пуста.

const isMobile = (testInfo) => testInfo.project.name === 'mobile-chromium';

async function openThread(page, threadId, query = '') {
  await page.goto(`/?mock=1${query}#/threads/${threadId}`);
  // Тред считается открытым, когда поток поднят и ввод разблокирован.
  await expect(page.locator('#composer-input')).toBeEnabled({ timeout: 5000 });
  return page.locator('.desktop-aside');
}

test.describe('«Квоты» в боковой панели треда', () => {
  test('бот на подписке Claude Code: только его метр, без Codex и Gemini', async ({ page }, testInfo) => {
    test.skip(isMobile(testInfo), 'панель «Квоты» только в десктопной раскладке');
    const aside = await openThread(page, 't-mac');

    await expect(aside.locator('.desktop-aside-label', { hasText: 'Квоты' })).toBeVisible();
    await expect(aside.locator('.meter-row')).toHaveCount(1);
    await expect(aside.locator('.meter-label')).toHaveText('Claude');
    await expect(aside.locator('[role="meter"]')).toHaveAttribute('aria-label', 'Claude: 42%');
    await expect(aside.locator('.meter-pct')).toHaveText('42%');
    await expect(aside.getByText('Codex')).toHaveCount(0);
    await expect(aside.getByText('Gemini')).toHaveCount(0);
  });

  test('бот на подписке Antigravity: метр Gemini, без Claude и Codex', async ({ page }, testInfo) => {
    test.skip(isMobile(testInfo), 'панель «Квоты» только в десктопной раскладке');
    const aside = await openThread(page, 't-scout');

    await expect(aside.locator('.meter-row')).toHaveCount(1);
    await expect(aside.locator('.meter-label')).toHaveText('Gemini');
    await expect(aside.locator('[role="meter"]')).toHaveAttribute('aria-label', 'Gemini: 7%');
    await expect(aside.getByText('Claude')).toHaveCount(0);
    await expect(aside.getByText('Codex')).toHaveCount(0);
  });

  test('бот на API-ключе (раннер Codex): свой расход за сегодня, без метров Claude/Codex', async ({ page }, testInfo) => {
    test.skip(isMobile(testInfo), 'панель «Квоты» только в десктопной раскладке');
    const aside = await openThread(page, 't-coder');

    // Единственный метр — расход бота (200 000 из 200 000 = 100%, стиль опасности), подписан именем бота.
    await expect(aside.locator('.meter-row')).toHaveCount(1);
    await expect(aside.locator('.meter-label')).toHaveText('Кодер');
    await expect(aside.locator('[role="meter"]')).toHaveAttribute('aria-label', /Кодер: 200[\u202F0-9]+ из 200[\u202F0-9]+ токенов/);
    await expect(aside.locator('[role="meter"]')).toHaveAttribute('aria-valuenow', '100');
    await expect(aside.locator('.meter-pct')).toHaveText('100%');
    await expect(aside.locator('.meter-fill')).toHaveAttribute('style', /background:var\(--danger-fg\)/);
    await expect(aside.getByText(/Сегодня: 200[\u202F0-9]+ из 200[\u202F0-9]+ токенов дневного бюджета/)).toBeVisible();

    await expect(aside.getByText('Claude')).toHaveCount(0);
    await expect(aside.getByText('Codex')).toHaveCount(0);
  });

  test('бот на API-ключе: видит расход за сегодня и месяц в долларах и токенах', async ({ page }, testInfo) => {
    test.skip(isMobile(testInfo), 'панель «Квоты» только в десктопной раскладке');
    const aside = await openThread(page, 't-coder');

    // Карточка с расходами перед метром дневного бюджета.
    const spendCard = aside.locator('.card.card-pad').filter({ hasText: 'Сегодня' });
    await expect(spendCard).toBeVisible();

    // Строка «Сегодня» содержит долларовую сумму и количество токенов.
    const todayRow = spendCard.locator('.row').filter({ hasText: 'Сегодня' });
    const todayText = await todayRow.innerText();
    expect(todayText).toMatch(/≈\s*\$\d+,\d{2}/);
    expect(todayText).toContain('токенов');

    // Строка «За месяц» содержит долларовую сумму.
    const monthRow = spendCard.locator('.row').filter({ hasText: 'За месяц' });
    const monthText = await monthRow.innerText();
    expect(monthText).toMatch(/≈\s*\$\d+,\d{2}/);
    expect(monthText).toContain('токенов');
  });

  test('бот на подписке Claude: не видит долларовых сумм в панели «Квоты»', async ({ page }, testInfo) => {
    test.skip(isMobile(testInfo), 'панель «Квоты» только в десктопной раскладке');
    const aside = await openThread(page, 't-mac');

    await expect(aside.locator('.meter-label')).toHaveText('Claude');
    // В режиме подписки нет карточки с расходами «Сегодня»/«За месяц».
    await expect(aside.getByText('Сегодня')).toHaveCount(0);
    await expect(aside.getByText('За месяц')).toHaveCount(0);
  });

  test('сводки расхода нет: бот на API-ключе видит дневной бюджет без квот подписок', async ({ page }, testInfo) => {
    test.skip(isMobile(testInfo), 'панель «Квоты» только в десктопной раскладке');
    const aside = await openThread(page, 't-coder', '&usage=none');

    await expect(aside.locator('.meter-row')).toHaveCount(1);
    await expect(aside.locator('.meter-row .meter-label')).toHaveText('Кодер');
    await expect(aside.locator('[role="meter"]')).toHaveAttribute('aria-valuenow', '0');
    await expect(aside.locator('.meter-row .meter-label').filter({ hasText: /^(Claude|Codex|Gemini)$/ })).toHaveCount(0);
    await expect(aside.getByText(/Сегодня: 0 из 200[\u202F0-9]+ токенов дневного бюджета/)).toBeVisible();
  });

  test('телефон: на экране треда панели «Квоты» нет', async ({ page }, testInfo) => {
    test.skip(!isMobile(testInfo), 'только мобильная раскладка');
    await page.goto('/?mock=1#/threads/t-mac');
    await expect(page.locator('#composer-input')).toBeEnabled({ timeout: 5000 });
    await expect(page.locator('.desktop-aside')).toHaveCount(0);
    await expect(page.getByText('Квоты')).toHaveCount(0);
  });
});
