import { expect, test } from '@playwright/test';

// Расписание типа hook: адрес Slack и секрет подписи Signing Secret (docs/contracts.md, раздел 18).
// Мок-режим: ?mock=1. Секрет только на запись: после сохранения поле пустое, виден лишь признак.

const block = (page) => page.locator('[data-slack-hook]');
const secret = (page) => page.locator('#sch-slack-secret');

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
