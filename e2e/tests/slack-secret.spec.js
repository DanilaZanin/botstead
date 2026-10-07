import { expect, test } from '@playwright/test';

// Расписание типа hook: адрес Slack и секрет подписи Signing Secret (docs/contracts.md, раздел 18).
// Мок-режим: ?mock=1. Секрет только на запись: после сохранения поле пустое, виден лишь признак.

const block = (page) => page.locator('[data-slack-hook]');
const secret = (page) => page.locator('[id^="sch-slack-secret-"]');

async function open(page, id = 's3') {
  await page.goto(`/?mock=1#/routines/${id}`);
  await expect(block(page)).toBeVisible();
}

test('hook-расписание показывает адрес Slack и поле секрета типа password', async ({ page }) => {
  await open(page);
  await expect(page.locator('[data-slack-url]')).toHaveText(/\/bots\/hooks\/s3\/slack$/);
  await expect(secret(page)).toHaveAttribute('type', 'password');
  await expect(block(page)).toContainText('Секрет не задан');
  await expect(page.getByRole('button', { name: 'Удалить секрет' })).toHaveCount(0);
});

test('секрет сохраняется без показа значения и очищается', async ({ page }) => {
  await open(page);
  await secret(page).fill('abc123signing');
  await page.getByRole('button', { name: 'Сохранить секрет' }).click();
  await expect(block(page)).toContainText('Секрет задан');
  await expect(secret(page)).toHaveValue('');
  await expect(page.locator('body')).not.toContainText('abc123signing');
  await page.getByRole('button', { name: 'Удалить секрет' }).click();
  await expect(block(page)).toContainText('Секрет не задан');
});

test('Bot Token сохраняется отдельно от уже заданного Signing Secret', async ({ page }) => {
  await open(page);
  await secret(page).fill('signing-123');
  await page.getByRole('button', { name: 'Сохранить секреты' }).click();
  await expect(block(page)).toContainText('Секрет задан');
  await page.locator('#sch-slack-token').fill('xoxb-token');
  await page.getByRole('button', { name: 'Сохранить секреты' }).click();
  await expect(block(page)).toContainText('Секрет задан');
  await expect(block(page)).toContainText('Токен задан');
});

test('блок Slack только у hook-расписаний', async ({ page }) => {
  await page.goto('/?mock=1#/routines/s1');
  await expect(page.getByText('Проверка серверов').first()).toBeVisible();
  await expect(page.locator('[data-slack-hook]')).toHaveCount(page.viewportSize().width >= 1024 ? 1 : 0);  // на Mac блок один: у единственного hook-расписания
});

test('английский интерфейс переводит подписи блока', async ({ page }) => {
  await page.addInitScript(() => { try { localStorage.setItem('bothub.lang', 'en'); } catch {} });
  await open(page);
  await expect(block(page)).toContainText('Slack signing secret');
  await expect(page.getByRole('button', { name: 'Save secret' })).toBeVisible();
});

test('адаптер Mailgun показывает адреса и ключ только на запись', async ({ page }) => {
  await open(page);
  await page.getByLabel('Адаптер вебхука').selectOption('email');
  const mailgun = page.locator('[data-hook-panel="email"]');
  await expect(mailgun).toBeVisible();
  await expect(mailgun).toContainText('/bots/hooks/s3/email/mock-email-token/json');
  await expect(mailgun).not.toContainText('/bots/hooks/s3/email/mock-hook-token/json');
  await expect(mailgun).not.toContainText('/bots/hooks/s3/email?token=mock-hook-token');
  await expect(mailgun).toContainText('остальные вебхуки этого расписания отключены');
  const key = mailgun.getByLabel('Ключ подписи Mailgun');
  await expect(key).toHaveAttribute('type', 'password');
  await key.fill('mailgun-signing-key');
  await mailgun.getByRole('button', { name: 'Сохранить ключ' }).click();
  await expect(page.locator('[data-hook-panel="email"]')).toContainText('Ключ задан');
  await expect(page.locator('[data-hook-panel="email"] input')).toHaveValue('');
  await expect(page.locator('body')).not.toContainText('mailgun-signing-key');
  await page.locator('[data-hook-panel="email"]').getByRole('button', { name: 'Удалить ключ' }).click();
  await expect(page.locator('[data-hook-panel="email"]')).toContainText('Ключ не задан');
});
