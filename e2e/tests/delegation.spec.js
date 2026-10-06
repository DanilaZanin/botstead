import { expect, test } from '@playwright/test';

// Поручения бота боту (docs/contracts.md, раздел 19): в треде получателя сообщение от другого бота помечено
// «От бота <имя>», в ленте активности два события. Мок-режим: ?mock=1. Оба проекта: телефон 393 px и Mac.
// В моке Скаут поручил Архиву разложить скан договора (тред Архива, t-archive).

const label = (page) => page.locator('[data-delegated-from]');

async function useLang(page, lang) {
  await page.addInitScript((value) => { try { localStorage.setItem('bothub.lang', value); } catch { /* приватный режим */ } }, lang);
}

test.describe('поручение в треде получателя', () => {
  test('сообщение от бота помечено «От бота Скаут», обычное сообщение без метки', async ({ page }) => {
    await page.goto('/?mock=1#/threads/t-archive');
    await expect(label(page)).toHaveCount(1);
    await expect(label(page)).toHaveText('От бота Скаут');
    const bubble = page.locator('.msg-user-wrap .msg-user');
    await expect(bubble).toContainText('Поручение от бота Скаут:');
    await expect(bubble).toContainText('Разложи скан договора из inbox по папкам');
    await page.goto('/?mock=1#/threads/t-scout');
    await expect(page.locator('.msg-user').first()).toBeVisible();
    await expect(label(page)).toHaveCount(0);
  });

  test('имя бота и текст поручения это данные: не переводятся, слово «От бота» переводится', async ({ page }) => {
    await useLang(page, 'en');
    await page.goto('/?mock=1#/threads/t-archive');
    await expect(label(page)).toHaveText('From bot Скаут');
    await expect(page.locator('.msg-user-wrap .msg-user')).toContainText('Разложи скан договора');
  });

  test('метка не раздувает пузырь: влезает в экран без горизонтальной прокрутки', async ({ page }) => {
    await page.goto('/?mock=1#/threads/t-archive');
    await expect(label(page)).toBeVisible();
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth);
    expect(overflow).toBe(false);
  });
});

test.describe('поручения в ленте активности', () => {
  test('показаны отправка и завершение с именами ботов и текстом задачи', async ({ page }) => {
    await page.goto('/?mock=1#/settings/activity');
    await expect(page.getByRole('heading', { level: 1, name: 'Активность' })).toBeVisible();
    const sent = page.locator('.act-row').filter({ hasText: 'Бот передал задачу другому боту' });
    await expect(sent).toHaveCount(1);
    await expect(sent).toContainText('Скаут → Архив');
    await expect(sent).toContainText('Разложи скан договора из inbox по папкам');
    const done = page.locator('.act-row').filter({ hasText: 'Поручение выполнено' });
    await expect(done).toHaveCount(1);
    await expect(done).toContainText('Скаут → Архив');
  });

  test('строки ведут в тред получателя', async ({ page }) => {
    await page.goto('/?mock=1#/settings/activity');
    await page.locator('.act-row').filter({ hasText: 'Бот передал задачу другому боту' }).click();
    await expect(page).toHaveURL(/#\/threads\/t-archive/);
  });
});
