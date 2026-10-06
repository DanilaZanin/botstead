import { expect, test } from '@playwright/test';

test.beforeEach(async ({ page }) => {
  await page.goto('/?mock=1');
  await expect(page.locator('[data-action="open-thread"][data-bot="scout"]')).toBeVisible();
});

async function openScout(page) {
  await page.locator('[data-action="open-thread"][data-bot="scout"]').first().click();
  await expect(page).toHaveURL(/#\/threads\/t-scout$/);
  await expect(page.locator('#composer-input')).toBeVisible();
}

test('bot list shows the seeded bots', async ({ page }) => {
  await expect(page.locator('[data-action="open-thread"]')).toHaveCount(5);
  await expect(page.getByText('Скаут', { exact: true }).first()).toBeVisible();
  await expect(page.getByText('Кодер', { exact: true }).first()).toBeVisible();
});

test('opens a bot thread', async ({ page }) => {
  await openScout(page);
  await expect(page.locator('#thread-body')).toContainText('Нашёл 7 вакансий SRE');
});

test('sends a message in a thread', async ({ page }) => {
  await openScout(page);
  const message = 'E2E: покажи короткий статус';
  await page.getByLabel('Сообщение').fill(message);
  await page.getByRole('button', { name: 'Отправить' }).click();
  await expect(page.locator('#thread-body')).toContainText(message, { timeout: 5_000 });
  await expect(page.locator('#thread-body')).toContainText(`ok: ${message}`, { timeout: 5_000 });
});

test('builds a bot draft and creates the bot', async ({ page }, testInfo) => {
  if (testInfo.project.name === 'desktop-chromium') {
    await page.goto('/?mock=1#/bots/new');
  } else {
    await page.getByRole('link', { name: 'Бот', exact: true }).click();
  }
  await expect(page).toHaveURL(/#\/bots\/new$/);
  await page.getByLabel('Что бот должен делать и как работать').fill('Каждый день проверяй новые вакансии SRE и присылай краткий список');
  await page.getByRole('button', { name: 'Собрать' }).click();
  await expect(page.getByLabel('Имя')).toBeVisible({ timeout: 5_000 });
  const name = await page.getByLabel('Имя').inputValue();
  await page.getByRole('button', { name: 'Создать' }).click();
  await expect(page).toHaveURL(/#\/threads\/t-draft-/);
  await expect(page.locator('#composer-input')).toBeVisible();
  await expect(page.locator('.desktop-thread-head, .app-header')).toContainText(name);
});

// Настройки бота открываются из треда (шестерёнка в шапке на телефоне, кнопка в шапке на Mac), одна форма на любой ширине.
async function openScoutSettings(page) {
  await openScout(page);
  await page.getByRole('link', { name: 'Настройки бота', exact: true }).click();
  await expect(page).toHaveURL(/#\/bots\/scout(\?from=.*)?$/);
}

test('opens bot settings', async ({ page }) => {
  await openScoutSettings(page);
  await expect(page.getByText('Настройки бота')).toBeVisible();
  await expect(page.getByText('Модель', { exact: true })).toBeVisible();
  // выбор модели из реестра провайдеров проверяется в providers.spec.js; здесь обычная настройка на месте
  const mac = page.getByRole('radio', { name: 'Mac', exact: true });
  await mac.click();
  await expect(mac).toHaveAttribute('aria-checked', 'true');
});

test('selects an avatar in bot settings', async ({ page }) => {
  await openScoutSettings(page);
  await expect(page.locator('.avatar-pick')).toHaveCount(10);
  const owl = page.getByRole('button', { name: 'Персонаж: Сова' });
  await expect(owl.locator('img')).toHaveAttribute('src', './avatars/owl.webp');
  await owl.click();
  await expect(owl).toHaveAttribute('aria-pressed', 'true');
  await expect(page.locator('.avatar-pick[aria-pressed="true"]')).toHaveCount(1);
  await expect(page.locator('[data-avatar-slot="scout"] img').first()).toHaveAttribute('src', './avatars/owl.webp');
});

// Шестерёнка в шапке «Боты» на телефоне и пункт «Настройки» в сайдбаре на Mac ведут в хаб.
test('settings gear opens the settings hub', async ({ page }) => {
  await page.getByRole('link', { name: 'Настройки', exact: true }).click();
  await expect(page).toHaveURL(/#\/settings$/);
  await expect(page.getByRole('heading', { name: 'Настройки' })).toBeVisible();
});

test('shows memory and accepts a proposed fact', async ({ page }) => {
  await page.getByRole('link', { name: 'Память' }).click();
  await expect(page).toHaveURL(/#\/memory$/);
  const proposed = page.getByText(/Резюме EN для откликов/);
  await expect(proposed).toBeVisible();
  await page.getByRole('button', { name: 'Запомнить' }).click();
  await expect(page.locator('[data-action="remember-yes"]')).toHaveCount(0);
  await expect(proposed).toBeVisible();
});

test('lists schedules and runs one', async ({ page }) => {
  await page.getByRole('link', { name: 'Рутины' }).click();
  await expect(page).toHaveURL(/#\/routines$/);
  await expect(page.getByRole('heading', { name: 'Рутины' }).or(page.getByText('Рутины', { exact: true }).first())).toBeVisible();
  await expect(page.getByText('Проверка серверов')).toBeVisible();
  await page.locator('[data-action="run-schedule"]').first().click();
  await expect(page.getByText('Проверка серверов')).toBeVisible();
});
