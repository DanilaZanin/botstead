import { expect, test } from '@playwright/test';

// Проверяющая модель действий (docs/contracts.md, раздел 20): поле в настройках бота и подсказка в карточке подтверждения.
// Мок-режим: ?mock=1. Состояние бота читается через window.__ctxMock.bot, события в тред кладёт window.__ctxMock.push.
// В моке у ap2 (бот SRE) вердикт ask, у ap1 (Скаут) проверки не было. Оба проекта: телефон 393 px и Mac.

const select = (page) => page.locator('#checker-model');
const saved = (page, bot = 'scout') => page.evaluate((id) => window.__ctxMock.bot(id).checker_model_id, bot);
const hint = (page, id) => page.locator(`[data-approval-id="${id}"] [data-checker-hint]`);

test.describe('проверяющая модель в настройках бота', () => {
  test('карточка с подсказкой, по умолчанию «Не задана», в списке только модели API-провайдеров', async ({ page }) => {
    await page.goto('/?mock=1#/bots/scout');
    await expect(page.getByText('Проверяющая модель', { exact: true })).toBeVisible();
    await expect(select(page)).toBeVisible();
    await expect(select(page).locator('option:checked')).toHaveText('Не задана');
    await expect(select(page)).toHaveAccessibleName('Проверяющая модель');
    const labels = await select(page).locator('option').allTextContents();
    expect(labels).toContain('Opus 5.5');
    expect(labels.join('|')).not.toContain('Gemini');  // подписки (Antigravity) не подходят
    expect(await saved(page)).toBe(null);
  });

  test('выбор сохраняется, после перезахода он на месте, «Не задана» выключает проверку', async ({ page }) => {
    await page.goto('/?mock=1#/bots/scout');
    await select(page).selectOption({ label: 'Opus 5.5' });
    await expect.poll(() => saved(page)).not.toBe(null);
    await expect(page.locator('[data-checker-alert] [role="alert"]')).toHaveCount(0);
    await page.evaluate(() => { location.hash = '#/'; });
    await expect(select(page)).toHaveCount(0);
    await page.evaluate(() => { location.hash = '#/bots/scout'; });
    await expect(select(page).locator('option:checked')).toHaveText('Opus 5.5');
    await select(page).selectOption({ label: 'Не задана' });
    await expect.poll(() => saved(page)).toBe(null);
  });
});

test.describe('подсказка проверяющей модели в карточке подтверждения', () => {
  test('вердикт и причина в треде: «Проверка: ask · …»; без проверки строки нет', async ({ page }) => {
    await page.goto('/?mock=1#/threads/t-sre');
    await expect(hint(page, 'ap2')).toHaveText('Проверка: ask · Перезапуск webapp не просили, подтвердите сами');
    await expect(page.locator('[data-approval-id="ap2"]').getByRole('button', { name: 'Разрешить' })).toBeVisible();
    await page.goto('/?mock=1#/threads/t-scout');
    await expect(page.locator('[data-approval-id="ap1"]')).toBeVisible();
    await expect(page.locator('[data-checker-hint]')).toHaveCount(0);
  });

  test('подсказка в событии approval_req вживую', async ({ page }) => {
    await page.goto('/?mock=1#/threads/t-scout');
    await expect(page.locator('[data-approval-id="ap1"]')).toBeVisible();
    await page.evaluate(() => window.__ctxMock.push('t-scout', 'approval_req', { approval_id: 'ap9', risk: 'send', title: 'Отправить письмо', tool: 'Bash', expires_at: new Date(Date.now() + 3600e3).toISOString(), checker: { verdict: 'allow', reason: 'Это просил владелец' } }, 'tu1'));
    await expect(hint(page, 'ap9')).toHaveText('Проверка: allow · Это просил владелец', { timeout: 5000 });
    await expect(page.locator('[data-approval-id="ap9"]').getByRole('button', { name: 'Разрешить' })).toBeVisible();  // allow ничего не решает сам
  });

  test('автоотказ: причина в карточке, кнопок нет, «Отклонено проверяющей моделью»', async ({ page }) => {
    await page.goto('/?mock=1#/threads/t-scout');
    await expect(page.locator('[data-approval-id="ap1"]')).toBeVisible();
    await page.evaluate(() => {
      window.__ctxMock.push('t-scout', 'approval_req', { approval_id: 'ap8', risk: 'pay', title: 'Оплатить подписку', tool: 'Bash', expires_at: new Date(Date.now() + 3600e3).toISOString(), checker: { verdict: 'deny', reason: 'Оплату не просили' } }, 'tu1');
      window.__ctxMock.push('t-scout', 'approval_dec', { approval_id: 'ap8', decision: 'rejected', remember: false, client: 'system', reason: 'checker_denied', checker: { verdict: 'deny', reason: 'Оплату не просили' } }, 'tu1');
    });
    const card = page.locator('[data-approval-id="ap8"]');
    await expect(card.locator('[data-approval-status]')).toHaveText('Отклонено проверяющей моделью', { timeout: 5000 });
    await expect(hint(page, 'ap8')).toHaveText('Проверка: deny · Оплату не просили');
    await expect(card.getByRole('button')).toHaveCount(0);
  });

  test('экран «Решения» показывает ту же строку', async ({ page }) => {
    await page.goto('/?mock=1#/approvals/ap2');
    await expect(page.locator('[data-checker-hint]')).toHaveText('Проверка: ask · Перезапуск webapp не просили, подтвердите сами');
  });
});
