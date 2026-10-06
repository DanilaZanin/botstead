import { expect, test } from '@playwright/test';

// Карточка «Пробуждения бота» в настройках бота (docs/contracts.md, раздел 17): список активных пробуждений,
// которые бот запланировал себе сам, отмена, пустое состояние, сбой загрузки и повтор.
// Мок-режим: ?mock=1, &wakeups=fail (первая загрузка падает, повтор проходит). Оба проекта: телефон 393 px и Mac.
// В моке у бота Скаут два активных пробуждения, у Архива ни одного.

const CARD = '[data-wakeups]';
const FIRST = 'Проверить отклики на вакансии';
const SECOND = 'Напомнить про собеседование';

const card = (page) => page.locator(CARD);
const rows = (page) => page.locator(`${CARD} .wk-row`);
const cancelButton = (page, reason) => page.getByRole('button', { name: `Отменить пробуждение: ${reason}` });

async function open(page, bot = 'scout', query = '') {
  await page.goto(`/?mock=1${query}#/bots/${bot}`);
  await expect(card(page)).toBeVisible();
}

test.describe('карточка «Пробуждения бота»', () => {
  test('показывает заголовок, пояснение и активные пробуждения по близости срока', async ({ page }) => {
    await open(page);
    await expect(card(page).getByRole('heading', { name: 'Пробуждения бота' })).toBeVisible();
    await expect(card(page)).toContainText('Бот сам просит разбудить его позже');
    await expect(rows(page)).toHaveCount(2);
    await expect(rows(page).nth(0).locator('.wk-reason')).toHaveText(FIRST);  // через 3 часа
    await expect(rows(page).nth(1).locator('.wk-reason')).toHaveText(SECOND);  // через 2 суток
    await expect(card(page).locator('.wk-empty, .wk-error')).toHaveCount(0);
  });

  test('причина помечена data-i18n-skip: это текст бота, его не переводят', async ({ page }) => {
    await open(page);
    await expect(rows(page).first().locator('.wk-reason')).toHaveAttribute('data-i18n-skip', '');
  });

  test('у каждой строки есть время и кнопка отмены с именем по причине', async ({ page }) => {
    await open(page);
    for (const reason of [FIRST, SECOND]) {
      await expect(cancelButton(page, reason)).toBeVisible();
      await expect(cancelButton(page, reason)).toBeEnabled();
    }
    await expect(rows(page).first().locator('.row-sub')).not.toBeEmpty();
  });

  test('отмена убирает только выбранную строку', async ({ page }) => {
    await open(page);
    await cancelButton(page, FIRST).click();
    await expect(rows(page)).toHaveCount(1);
    await expect(rows(page).first().locator('.wk-reason')).toHaveText(SECOND);
    await expect(cancelButton(page, FIRST)).toHaveCount(0);
  });

  test('после отмены обоих строк карточка пишет, что пробуждений нет', async ({ page }) => {
    await open(page);
    await cancelButton(page, FIRST).click();
    await expect(rows(page)).toHaveCount(1);
    await cancelButton(page, SECOND).click();
    await expect(rows(page)).toHaveCount(0);
    await expect(card(page).locator('.wk-empty')).toHaveText('Бот пока не планировал пробуждений');
  });

  test('отменённое пробуждение не возвращается при повторном открытии экрана', async ({ page }) => {
    await open(page);
    await cancelButton(page, FIRST).click();
    await expect(rows(page)).toHaveCount(1);
    await page.evaluate(() => { location.hash = '#/settings'; });
    await page.evaluate(() => { location.hash = '#/bots/scout'; });
    await expect(card(page)).toBeVisible();
    await expect(rows(page)).toHaveCount(1);
    await expect(rows(page).first().locator('.wk-reason')).toHaveText(SECOND);
  });

  test('у бота без пробуждений пустое состояние и нет кнопок отмены', async ({ page }) => {
    await open(page, 'archive');
    await expect(card(page).locator('.wk-empty')).toHaveText('Бот пока не планировал пробуждений');
    await expect(rows(page)).toHaveCount(0);
    await expect(card(page).locator('[data-wk-cancel]')).toHaveCount(0);
  });

  test('пробуждения другого бота не попадают в карточку', async ({ page }) => {
    await open(page, 'coder');
    await expect(card(page)).not.toContainText(FIRST);
    await expect(card(page)).not.toContainText(SECOND);
  });
});

test.describe('карточка «Пробуждения бота»: сбой загрузки', () => {
  test('показывает ошибку с кнопкой «Повторить», повтор грузит список', async ({ page }) => {
    await open(page, 'scout', '&wakeups=fail');
    await expect(card(page).locator('.wk-error')).toHaveText('Не удалось загрузить пробуждения');
    await expect(rows(page)).toHaveCount(0);
    await expect(card(page).getByRole('button', { name: 'Повторить' })).toBeVisible();
    await card(page).getByRole('button', { name: 'Повторить' }).click();
    await expect(rows(page)).toHaveCount(2);
    await expect(card(page).locator('.wk-error')).toHaveCount(0);
  });

  test('ошибка объявляется скринридеру как предупреждение', async ({ page }) => {
    await open(page, 'scout', '&wakeups=fail');
    await expect(card(page).locator('.wk-error')).toHaveAttribute('role', 'alert');
  });
});

test.describe('карточка «Пробуждения бота»: вёрстка', () => {
  test('не вылезает за экран и не даёт горизонтальной прокрутки', async ({ page }) => {
    await open(page);
    await expect(rows(page)).toHaveCount(2);
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow).toBeLessThanOrEqual(0);
    const box = await card(page).boundingBox();
    const viewport = page.viewportSize();
    expect(box.x).toBeGreaterThanOrEqual(0);
    expect(box.x + box.width).toBeLessThanOrEqual(viewport.width);
  });
});
