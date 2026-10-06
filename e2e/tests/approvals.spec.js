import { expect, test } from '@playwright/test';

// Карточка одобрения в треде: устаревшее решение (409) и оборванный ход.
// Мок-режим: &decide=409 (решение отвечает 409, одобрение уже истекло), &decide=409-decided (уже решено в другом месте).
// События в тред подкладывает window.__ctxMock.push(threadId, kind, payload, turnId), состояние одобрений меняет
// window.__procMock.approvals.

const THREAD = '#/threads/t-scout';
const card = (page) => page.locator('[data-approval-id="ap1"]');
const status = (page) => card(page).locator('[data-approval-status]');

async function openCard(page, query = '') {
  await page.goto(`/?mock=1${query}${THREAD}`);
  await expect(card(page)).toBeVisible();
  await expect(card(page).getByRole('button', { name: 'Разрешить' })).toBeVisible();
}

test.describe('карточка одобрения в треде', () => {
  test('решение принято: «Разрешено», кнопок нет', async ({ page }) => {
    await openCard(page);
    await card(page).getByRole('button', { name: 'Разрешить' }).click();
    await expect(status(page)).toHaveText('Разрешено');
    await expect(card(page).getByRole('button')).toHaveCount(0);
  });

  test('409, одобрение истекло: карточка перечитывается и говорит «Срок вышел» без кнопок', async ({ page }) => {
    await openCard(page, '&decide=409');
    await card(page).getByRole('button', { name: 'Разрешить' }).click();
    await expect(status(page)).toBeVisible();
    await expect(status(page)).toHaveText('Срок вышел');
    await expect(card(page).getByRole('button')).toHaveCount(0);
    // список ожидающих тоже обновился: ap1 там больше нет
    const pending = await page.evaluate(() => window.__procMock.approvals.filter((a) => a.status === 'pending').map((a) => a.id));
    expect(pending).not.toContain('ap1');
  });

  test('409, решено в другом месте: «Уже решено» без кнопок', async ({ page }) => {
    await openCard(page, '&decide=409-decided');
    await card(page).getByRole('button', { name: 'Отклонить' }).click();
    await expect(status(page)).toHaveText('Уже решено');
    await expect(card(page).getByRole('button')).toHaveCount(0);
  });

  test('«Срок вышел» из события approval_dec ядра', async ({ page }) => {
    await openCard(page);
    await page.evaluate(() => window.__ctxMock.push('t-scout', 'approval_dec', { approval_id: 'ap1', decision: 'expired', remember: false, client: 'system' }, 'tu1'));
    await expect(status(page)).toHaveText('Срок вышел', { timeout: 5000 });
    await expect(card(page).getByRole('button')).toHaveCount(0);
  });

  test('ход оборвался без события approval_dec (перезапуск ядра): плашка «Ход прерван» и карточка перечитана', async ({ page }) => {
    await openCard(page);
    await page.evaluate(() => {
      window.__procMock.approvals.find((a) => a.id === 'ap1').status = 'expired';
      window.__ctxMock.push('t-scout', 'approval_expired', { turn_id: 'tu1', approval_id: 'ap1', tool: 'browser.submit_form', detail: 'approval истёк, turn завершён' }, 'tu1');
      window.__ctxMock.push('t-scout', 'status', { turn_id: 'tu1', status: 'error' }, 'tu1');
    });
    const plaque = page.locator('[data-turn-error="tu1"]');
    await expect(plaque).toBeVisible({ timeout: 5000 });
    await expect(plaque).toHaveText('Ход прерван: срок одобрения вышел');
    await expect(status(page)).toHaveText('Срок вышел');
    await expect(card(page).getByRole('button')).toHaveCount(0);
  });

  test('status error с причиной в payload: «Ход прерван: <причина>», без причины просто «Ход прерван»; повтор не дублирует', async ({ page }) => {
    await openCard(page);
    await page.evaluate(() => {
      window.__ctxMock.push('t-scout', 'status', { turn_id: 'tu-a', status: 'error', reason: 'лимит токенов' }, 'tu-a');
      window.__ctxMock.push('t-scout', 'status', { turn_id: 'tu-b', status: 'error' }, 'tu-b');
      window.__ctxMock.push('t-scout', 'status', { turn_id: 'tu-b', status: 'error' }, 'tu-b');
    });
    await expect(page.locator('[data-turn-error="tu-a"]')).toHaveText('Ход прерван: лимит токенов', { timeout: 5000 });
    await expect(page.locator('[data-turn-error="tu-b"]')).toHaveText('Ход прерван');
    await expect(page.locator('[data-turn-error="tu-b"]')).toHaveCount(1);
    // ожидающая карточка, которой ход не касался, остаётся с кнопками
    await expect(card(page).getByRole('button', { name: 'Разрешить' })).toBeVisible();
  });

  test('ход остановлен вручную: плашки «Ход прерван» нет', async ({ page }) => {
    await openCard(page);
    await page.evaluate(() => window.__ctxMock.push('t-scout', 'status', { turn_id: 'tu-s', status: 'stopped' }, 'tu-s'));
    await page.waitForTimeout(2200);
    await expect(page.locator('[data-turn-error]')).toHaveCount(0);
  });
});

test.describe('экран «Решения»: 409', () => {
  test('решение по уже истёкшему одобрению: список перечитывается, ошибки нет', async ({ page }) => {
    await page.goto('/?mock=1&decide=409#/approvals');
    const pendingCount = () => page.evaluate(() => window.__procMock.approvals.filter((a) => a.status === 'pending').length);
    const before = await pendingCount();
    expect(before).toBeGreaterThan(1);
    await page.getByRole('button', { name: 'Разрешить' }).first().click();
    await expect.poll(pendingCount).toBe(before - 1);
    await expect(page.getByRole('alert')).toHaveCount(0);
  });
});
