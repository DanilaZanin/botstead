import { expect, test } from '@playwright/test';

// Блок «Подсказки» на главной (mobile) и на экране «#/» (desktop): карточки из GET /api/suggestions,
// кнопки «Сделать» (POST /accept, переводит в тред) и «Скрыть» (POST /dismiss, убирает карточку).
// Мок-режим: ?mock=1. &suggestions=none — пустой список, &suggestions=fail — ошибка сети.
// Оба проекта: телефон 393 px и Mac.

const FIRST = 'Создать напоминание о дедлайне проекта в пятницу';
const SECOND = 'Прогнать линтер перед коммитом в репозиторий bothub';

const isMobile = (testInfo) => testInfo.project.name === 'mobile-chromium';

function acceptBtns(page) { return page.locator('[data-action="accept-suggestion"]'); }
function acceptBtn(page, id) { return page.locator(`[data-action="accept-suggestion"][data-id="${id}"]`); }
function dismissBtn(page, id) { return page.locator(`[data-action="dismiss-suggestion"][data-id="${id}"]`); }
function cardByAccept(page, id) { return acceptBtn(page, id).locator('xpath=ancestor::article'); }

async function openMobile(page, query = '') {
  await page.goto(`/?mock=1${query}`);
  await expect(page.getByRole('heading', { name: 'Боты', level: 1 })).toBeVisible({ timeout: 5000 });
}

async function openDesktop(page, query = '') {
  await page.goto(`/?mock=1${query}#/`);
  await expect(page.getByRole('heading', { name: 'Подсказки', level: 1 })).toBeVisible({ timeout: 5000 });
}

test.describe('блок «Подсказки»', () => {
  test('показывает заголовок и две карточки с текстом кнопок', async ({ page }, testInfo) => {
    if (isMobile(testInfo)) await openMobile(page);
    else await openDesktop(page);

    await expect(page.getByText('Подсказки').first()).toBeVisible();
    await expect(acceptBtns(page)).toHaveCount(2);
    await expect(page.getByText(FIRST)).toBeVisible();
    await expect(page.getByText(SECOND)).toBeVisible();
    // Имя бота в подзаголовке карточки.
    await expect(cardByAccept(page, 's1').locator('.t-footnote')).toHaveText('Скаут');
    await expect(cardByAccept(page, 's2').locator('.t-footnote')).toHaveText('Кодер');
  });

  test('текст подсказки помечен data-i18n-skip (это текст бота)', async ({ page }, testInfo) => {
    if (isMobile(testInfo)) await openMobile(page);
    else await openDesktop(page);
    const firstBody = cardByAccept(page, 's1').locator('.t-body');
    await expect(firstBody).toHaveAttribute('data-i18n-skip', '');
  });

  test('«Сделать»: переходит в тред бота', async ({ page }, testInfo) => {
    if (isMobile(testInfo)) await openMobile(page);
    else await openDesktop(page);

    await acceptBtn(page, 's1').click();
    // accept возвращает thread_id — переходим к треду, composer доступен.
    await expect(page.locator('#composer-input')).toBeEnabled({ timeout: 5000 });
  });

  test('«Скрыть»: убирает карточку, остаётся одна', async ({ page }, testInfo) => {
    if (isMobile(testInfo)) await openMobile(page);
    else await openDesktop(page);

    await dismissBtn(page, 's1').click();
    await expect(acceptBtns(page)).toHaveCount(1);
    await expect(page.getByText(SECOND)).toBeVisible();
  });

  test('«Скрыть» на обеих карточках — блок исчезает (mobile) или пишет «Подсказок пока нет.» (desktop)', async ({ page }, testInfo) => {
    if (isMobile(testInfo)) {
      await openMobile(page);
      await dismissBtn(page, 's1').click();
      await dismissBtn(page, 's2').click();
      await expect(page.getByText('Подсказки')).toHaveCount(0);
    } else {
      await openDesktop(page);
      await dismissBtn(page, 's1').click();
      await dismissBtn(page, 's2').click();
      await expect(page.getByText('Подсказок пока нет.')).toBeVisible();
    }
  });

  test('скрытая подсказка не возвращается при повторном открытии экрана', async ({ page }, testInfo) => {
    if (isMobile(testInfo)) {
      await openMobile(page);
      await dismissBtn(page, 's1').click();
      await expect(acceptBtns(page)).toHaveCount(1);
      await page.evaluate(() => { location.hash = '#/settings'; });
      await page.evaluate(() => { location.hash = '#/'; });
      await expect(page.getByRole('heading', { name: 'Боты', level: 1 })).toBeVisible();
      await expect(acceptBtns(page)).toHaveCount(1);
    } else {
      await openDesktop(page);
      await dismissBtn(page, 's1').click();
      await expect(acceptBtns(page)).toHaveCount(1);
      await page.evaluate(() => { location.hash = '#/settings'; });
      await page.evaluate(() => { location.hash = '#/'; });
      await expect(page.getByRole('heading', { name: 'Подсказки', level: 1 })).toBeVisible();
      await expect(acceptBtns(page)).toHaveCount(1);
    }
  });
});

test.describe('блок «Подсказки»: пустой список', () => {
  test('нет подсказок — блок не показывается (mobile)', async ({ page }, testInfo) => {
    test.skip(!isMobile(testInfo), 'только мобильная раскладка');
    await openMobile(page, '&suggestions=none');
    await expect(page.getByText('Подсказки')).toHaveCount(0);
    await expect(acceptBtns(page)).toHaveCount(0);
  });

  test('нет подсказок — сообщение «Подсказок пока нет.» (desktop)', async ({ page }, testInfo) => {
    test.skip(isMobile(testInfo), 'только десктопная раскладка');
    await openDesktop(page, '&suggestions=none');
    await expect(page.getByText('Подсказок пока нет.')).toBeVisible();
    await expect(acceptBtns(page)).toHaveCount(0);
  });
});

test.describe('блок «Подсказки»: ошибка сети', () => {
  test('ошибка загрузки показывает сообщение с кнопкой «На главную»', async ({ page }) => {
    await page.goto('/?mock=1&suggestions=fail');
    await expect(page.getByText('Не получилось загрузить экран.')).toBeVisible({ timeout: 5000 });
    await expect(page.getByText('Server error')).toBeVisible();
    await expect(page.getByRole('button', { name: 'На главную' })).toBeVisible();
  });
});
