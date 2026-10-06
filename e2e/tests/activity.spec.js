import { expect, test } from '@playwright/test';

// Экран «Активность» (docs/contracts.md, раздел 16): состояние ботов с паузой и «Пауза всех», лента по дням с
// фильтрами по боту и виду, подгрузка по курсору, значок риска, переходы, метки пропусков на экране рутин.
// Мок-режим: ?mock=1, &activity=none|fail|slow|more-fail|xss, &pause=fail. Оба проекта: телефон 393 px и Mac.

const isMobile = (testInfo) => testInfo.project.name === 'mobile-chromium';
const ROUTE = '/?mock=1#/settings/activity';

const rows = (page) => page.locator('.act-row');
const botRows = (page) => page.locator('.act-bot');
const botFilter = (page) => page.getByRole('group', { name: 'Фильтр по боту' });
const kindFilter = (page) => page.getByRole('group', { name: 'Фильтр по виду' });
const toggle = (page, bot) => page.getByRole('switch', { name: `Пауза бота ${bot}` });

async function open(page, query = '') {
  await page.goto(`/?mock=1${query}#/settings/activity`);
  await expect(page.getByRole('heading', { level: 1, name: 'Активность' })).toBeVisible();
}

// Адрес меняется сразу, а новый экран рисуется после запросов. Если вернуться назад до этого, запоздавшая отрисовка
// ушедшего экрана ложится поверх экрана «Активность»: перед goBack ждём, пока старый экран заменят.
const leftScreen = (page) => expect(page.locator('#act')).toHaveCount(0);

test.describe('экран «Активность»: вход и состояния бота', () => {
  test('строка настроек ведёт на рабочий экран, а не на заглушку', async ({ page }) => {
    await page.goto('/?mock=1#/settings');
    const row = page.locator('a[href="#/settings/activity"]');  // на Mac ссылка «Активность» есть и в боковой панели
    await expect(row).not.toHaveClass(/is-soon/);
    await expect(row.locator('.soon-pill')).toHaveCount(0);
    await row.click();
    await expect(page).toHaveURL(/#\/settings\/activity$/);
    await expect(page.getByRole('heading', { level: 1, name: 'Активность' })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Раздел в разработке' })).toHaveCount(0);
    await expect(page.getByRole('heading', { name: 'Состояние ботов' })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Журнал действий' })).toBeVisible();
  });

  test('адрес #/activity открывает тот же экран', async ({ page }) => {
    await page.goto('/?mock=1#/activity');
    await expect(page.getByRole('heading', { level: 1, name: 'Активность' })).toBeVisible();
    await expect(botRows(page)).toHaveCount(5);
  });

  test('у каждого бота переключатель паузы, по умолчанию выключен', async ({ page }) => {
    await open(page);
    await expect(botRows(page)).toHaveCount(5);
    for (const name of ['Скаут', 'Мак', 'SRE', 'Кодер', 'Архив']) {
      await expect(toggle(page, name)).toHaveAttribute('aria-checked', 'false');
    }
    await expect(botRows(page).first()).toContainText('Идёт задача');  // Скаут ждёт решения
    await expect(page.getByRole('button', { name: 'Пауза всех' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Возобновить всех' })).toHaveCount(0);
  });

  test('имена ботов и тексты событий помечены data-i18n-skip', async ({ page }) => {
    await open(page);
    await expect(botRows(page).first().locator('.row-title')).toHaveAttribute('data-i18n-skip', '');
    await expect(rows(page).first()).toBeVisible();
    await expect(rows(page).first().locator('.row-meta [data-i18n-skip]')).toHaveCount(1);
    await expect(page.locator('.act-chip[data-chip="bot"][data-value="scout"]')).toHaveAttribute('data-i18n-skip', '');
  });
});

test.describe('экран «Активность»: лента', () => {
  test('события сгруппированы по дням, новые сверху, у подтверждения значок риска', async ({ page }) => {
    await open(page);
    await expect(page.getByRole('heading', { name: 'Сегодня' })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Вчера' })).toBeVisible();
    const first = rows(page).first();
    await expect(first).toContainText('Запрошено подтверждение');
    await expect(first.locator('.badge')).toHaveText('ОТПРАВКА');
    await expect(first).toContainText('Отправить отклик на «Senior SRE, Belgrade/remote»');
    const pay = rows(page).filter({ hasText: 'Оплатить подписку на хостинг' }).first();
    await expect(pay.locator('.badge')).toHaveText('ОПЛАТА');
    const times = await page.locator('.act-row time').evaluateAll((nodes) => nodes.map((n) => n.getAttribute('datetime')));
    expect([...times].sort().reverse()).toEqual(times);
  });

  test('значения введённых полей в ленте не показываются', async ({ page }) => {
    await open(page);
    await expect(rows(page).filter({ hasText: 'Заполнено поле' })).toHaveCount(1);
    await expect(rows(page).filter({ hasText: 'Заполнено поле' }).first()).not.toContainText('[redacted]');
    await expect(rows(page).filter({ hasText: 'Открыта страница' }).first()).toContainText('https://jobs.example.eu/812');
  });

  test('«Показать ещё» подгружает страницы по курсору без повторов', async ({ page }) => {
    await open(page);
    await expect(rows(page)).toHaveCount(30);
    const more = page.getByRole('button', { name: 'Показать ещё' });
    await more.click();
    await expect(rows(page)).toHaveCount(60);
    await more.click();
    await expect(rows(page)).toHaveCount(90);
    await more.click();
    await expect(rows(page)).toHaveCount(92);
    await expect(more).toHaveCount(0);
    const ids = await rows(page).evaluateAll((nodes) => nodes.map((n) => n.getAttribute('data-item')));
    expect(new Set(ids).size).toBe(ids.length);
  });

  test('после «Показать ещё» фокус на первой новой строке', async ({ page }) => {
    await open(page);
    await page.getByRole('button', { name: 'Показать ещё' }).click();
    await expect(rows(page)).toHaveCount(60);
    await expect(page.locator('.act-row[data-idx="30"]')).toBeFocused();
  });

  test('ошибка догрузки показана рядом с кнопкой, повтор работает', async ({ page }) => {
    await open(page, '&activity=more-fail');
    await page.getByRole('button', { name: 'Показать ещё' }).click();
    await expect(page.getByRole('alert').filter({ hasText: 'Не удалось загрузить ещё' })).toBeVisible();
    await expect(rows(page)).toHaveCount(30);
    await page.getByRole('button', { name: 'Показать ещё' }).click();
    await expect(rows(page)).toHaveCount(60);
  });

  test('фильтр по боту', async ({ page }) => {
    await open(page);
    const chip = botFilter(page).getByRole('button', { name: 'Мак' });
    await expect(botFilter(page).getByRole('button', { name: 'Все боты' })).toHaveAttribute('aria-pressed', 'true');
    await chip.click();
    await expect(chip).toHaveAttribute('aria-pressed', 'true');
    await expect(chip).toBeFocused();
    await expect(botFilter(page).getByRole('button', { name: 'Все боты' })).toHaveAttribute('aria-pressed', 'false');
    await expect(rows(page).first()).toBeVisible();
    const metas = await page.locator('.act-row .row-meta').evaluateAll((nodes) => nodes.map((n) => n.textContent));
    expect(metas.length).toBeGreaterThan(5);
    expect(metas.every((text) => text.startsWith('Мак'))).toBe(true);
    await botFilter(page).getByRole('button', { name: 'Все боты' }).click();
    await expect(rows(page)).toHaveCount(30);
  });

  test('фильтр по виду: несколько видов сразу и сброс', async ({ page }) => {
    await open(page);
    await kindFilter(page).getByRole('button', { name: 'Решения' }).click();
    await expect(rows(page).first()).toBeVisible();
    expect(await rows(page).evaluateAll((nodes) => nodes.every((n) => n.getAttribute('data-kind') === 'approval'))).toBe(true);
    await kindFilter(page).getByRole('button', { name: 'Память' }).click();
    await expect(rows(page).filter({ hasText: 'Бот предложил запомнить' })).toHaveCount(1);
    expect(await rows(page).evaluateAll((nodes) => nodes.every((n) => ['approval', 'memory'].includes(n.getAttribute('data-kind'))))).toBe(true);
    await expect(kindFilter(page).getByRole('button', { name: 'Все виды' })).toHaveAttribute('aria-pressed', 'false');
    await kindFilter(page).getByRole('button', { name: 'Все виды' }).click();
    await expect(kindFilter(page).getByRole('button', { name: 'Решения' })).toHaveAttribute('aria-pressed', 'false');
    await expect(rows(page)).toHaveCount(30);
  });

  test('пустой результат фильтра: сообщение и сброс', async ({ page }) => {
    await open(page);
    await botFilter(page).getByRole('button', { name: 'Архив' }).click();
    await kindFilter(page).getByRole('button', { name: 'Решения' }).click();
    await expect(page.getByRole('heading', { name: 'Ничего не найдено' })).toBeVisible();
    await page.getByRole('button', { name: 'Сбросить фильтры' }).click();
    await expect(rows(page)).toHaveCount(30);
    await expect(botFilter(page).getByRole('button', { name: 'Все боты' })).toHaveAttribute('aria-pressed', 'true');
  });

  test('строки ведут в тред и в запуск процедуры', async ({ page }) => {
    await open(page);
    await rows(page).first().click();
    await expect(page).toHaveURL(/#\/threads\/t-scout$/);
    await leftScreen(page);
    await page.goBack();
    await expect(page).toHaveURL(/#\/settings\/activity$/);
    await expect(rows(page).first()).toBeVisible();
    const chip = kindFilter(page).getByRole('button', { name: 'Процедуры' });
    await chip.click();
    await expect(chip).toHaveAttribute('aria-pressed', 'true');
    await rows(page).filter({ hasText: 'Процедура завершена' }).first().click();
    await expect(page).toHaveURL(/#\/procedure-runs\/run1$/);
  });

  test('рутина без треда ведёт на экран рутины, запись памяти без ссылки', async ({ page }) => {
    await open(page);
    const skipped = rows(page).filter({ hasText: 'Запуск пропущен: компьютер бота недоступен' });
    await expect(skipped).toHaveCount(1);
    await expect(skipped).toContainText('Проверка серверов');
    await expect(skipped).toContainText('Расписание на паузе, возобновится автоматически');
    await skipped.click();
    await expect(page).toHaveURL(/#\/routines\/s1$/);
    await leftScreen(page);
    await page.goBack();
    await expect(rows(page).first()).toBeVisible();
    const memory = rows(page).filter({ hasText: 'Бот предложил запомнить' });
    await expect(memory).toHaveCount(1);
    expect(await memory.evaluate((node) => node.tagName)).toBe('DIV');
  });

  test('загрузка, пустая лента и ошибка с повтором', async ({ page }) => {
    await open(page, '&activity=slow');
    await expect(page.getByText('Загружаем журнал…')).toBeVisible();
    await expect(rows(page).first()).toBeVisible();

    await open(page, '&activity=none');
    await expect(page.getByRole('heading', { name: 'Пока нет событий' })).toBeVisible();
    await expect(rows(page)).toHaveCount(0);

    await open(page, '&activity=fail');
    await expect(page.getByRole('alert').filter({ hasText: 'Не удалось загрузить журнал' })).toBeVisible();
    await page.getByRole('button', { name: 'Повторить' }).click();
    await expect(rows(page).first()).toBeVisible();
  });

  test('тексты событий с разметкой выводятся как текст', async ({ page }) => {
    await open(page, '&activity=xss');
    await expect(rows(page).first()).toContainText('<img src=x onerror=');
    await expect(page.locator('#app img[src="x"]')).toHaveCount(0);
    expect(await page.evaluate(() => window.__activityXss)).toBeUndefined();
  });
});

test.describe('экран «Активность»: пауза', () => {
  test('пауза бота: переключатель, подсказка про идущую задачу, событие в ленте', async ({ page }) => {
    await open(page);
    const sw = toggle(page, 'Скаут');
    await sw.click();
    await expect(sw).toHaveAttribute('aria-checked', 'true');
    await expect(sw).toBeFocused();
    await expect(botRows(page).first()).toContainText('На паузе');
    const banner = page.locator('[data-notice-turn]');
    await expect(banner).toContainText('Бот на паузе, но текущая задача продолжает работать');
    await expect(banner.getByRole('button', { name: 'Остановить текущую задачу' })).toBeVisible();
    await expect(rows(page).first()).toContainText('Бот поставлен на паузу');
    await expect(rows(page).first()).toContainText('Скаут');

    await banner.getByRole('button', { name: 'Остановить текущую задачу' }).click();
    await expect(banner).toHaveCount(0);
    await expect(sw).toHaveAttribute('aria-checked', 'true');  // остановка задачи пауза не снимает

    await sw.click();
    await expect(sw).toHaveAttribute('aria-checked', 'false');
    await expect(rows(page).first()).toContainText('Бот возобновил работу');
  });

  test('бот без идущей задачи: подсказки нет', async ({ page }) => {
    await open(page);
    await toggle(page, 'Мак').click();
    await expect(toggle(page, 'Мак')).toHaveAttribute('aria-checked', 'true');
    await expect(page.locator('[data-notice-turn]')).toHaveCount(0);
  });

  test('«Пауза всех» просит подтверждения и подсказывает про идущие задачи', async ({ page }) => {
    await open(page);
    await page.getByRole('button', { name: 'Пауза всех' }).click();
    const dialog = page.getByRole('dialog');
    await expect(dialog.getByRole('heading', { name: 'Поставить всех ботов на паузу?' })).toBeVisible();
    await expect(dialog).toContainText('Сейчас идёт задача, пауза её не остановит. Остановить можно отдельно.');
    await expect(dialog).toContainText('Скаут, SRE');
    await expect(dialog.getByRole('button', { name: 'Отмена' })).toBeFocused();
    await dialog.getByRole('button', { name: 'Отмена' }).click();
    await expect(dialog).toHaveCount(0);
    await expect(toggle(page, 'Скаут')).toHaveAttribute('aria-checked', 'false');

    await page.getByRole('button', { name: 'Пауза всех' }).click();
    await page.getByRole('dialog').getByRole('button', { name: 'Пауза всех' }).click();
    await expect(page.getByRole('dialog')).toHaveCount(0);
    for (const name of ['Скаут', 'Мак', 'SRE', 'Кодер', 'Архив']) {
      await expect(toggle(page, name)).toHaveAttribute('aria-checked', 'true');
    }
    await expect(page.locator('[data-notice-turn]')).toHaveCount(2);  // Скаут и SRE: задача идёт
    await expect(page.getByRole('button', { name: 'Пауза всех' })).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Возобновить всех' })).toBeVisible();
    await expect(rows(page).first()).toContainText('Бот поставлен на паузу');

    await page.getByRole('button', { name: 'Возобновить всех' }).click();
    for (const name of ['Скаут', 'Мак', 'SRE', 'Кодер', 'Архив']) {
      await expect(toggle(page, name)).toHaveAttribute('aria-checked', 'false');
    }
    await expect(page.locator('[data-notice-turn]')).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Пауза всех' })).toBeVisible();
  });

  test('«Пауза всех»: необязательная причина до 200 символов видна в журнале и у бота', async ({ page }) => {
    await open(page);
    await page.getByRole('button', { name: 'Пауза всех' }).click();
    const dialog = page.getByRole('dialog');
    const reason = dialog.getByLabel('Причина (необязательно)');
    await expect(reason).toHaveAttribute('maxlength', '200');
    await expect(dialog.getByRole('button', { name: 'Отмена' })).toBeFocused();  // поле не перехватывает фокус у «Отмена»
    await reason.fill('Отпуск до понедельника');
    await dialog.getByRole('button', { name: 'Пауза всех' }).click();
    await expect(page.getByRole('dialog')).toHaveCount(0);
    await expect(botRows(page).first()).toContainText('Отпуск до понедельника');
    await expect(rows(page).first()).toContainText('Бот поставлен на паузу');
    await expect(rows(page).first()).toContainText('Отпуск до понедельника');
  });

  test('отказ сервера: переключатель остаётся прежним, сообщение видно', async ({ page }) => {
    await open(page, '&pause=fail');
    await toggle(page, 'Скаут').click();
    await expect(page.getByRole('alert').filter({ hasText: 'Не удалось изменить паузу' })).toBeVisible();
    await expect(toggle(page, 'Скаут')).toHaveAttribute('aria-checked', 'false');
    await expect(toggle(page, 'Скаут')).toBeEnabled();
  });
});

test.describe('экран «Активность»: рутины и раскладка', () => {
  test('на экране рутин видны метки пропусков и «Возобновится автоматически»', async ({ page }) => {
    await page.goto('/?mock=1#/routines');
    await expect(page.getByText('Проверка серверов')).toBeVisible();
    await expect(page.locator('[data-skip-note]').filter({ hasText: 'Пропущено 6 раз: компьютер бота недоступен' })).toBeVisible();
    await expect(page.locator('[data-skip-auto]')).toHaveCount(1);
    await expect(page.locator('[data-skip-auto]')).toHaveText('Возобновится автоматически');
  });

  test('экран рутины: метка и переключатель «Один запуск после возобновления»', async ({ page }, testInfo) => {
    test.skip(!isMobile(testInfo), 'на Mac рутины показаны списком, отдельного экрана нет');
    await page.goto('/?mock=1#/routines/s1');
    await expect(page.locator('[data-skip-note]')).toContainText('Пропущено 6 раз');
    const sw = page.getByRole('switch', { name: /Один запуск после возобновления/ });
    await expect(sw).not.toBeChecked();
    await sw.click();
    await expect(sw).toBeChecked();
  });

  test('зоны касания не меньше 44 px, горизонтального скролла нет', async ({ page }) => {
    await open(page);
    await expect(rows(page).first()).toBeVisible();
    const selectors = ['.act-chip', '.act-switch', '[data-act="pause-all"]', '[data-act="more"]', '.act-row'];
    for (const selector of selectors) {
      const boxes = await page.locator(selector).evaluateAll((nodes) => nodes.map((n) => { const r = n.getBoundingClientRect(); return [Math.round(r.width), Math.round(r.height)]; }));
      expect(boxes.length, selector).toBeGreaterThan(0);
      for (const [width, height] of boxes) {
        expect(height, `${selector} высота`).toBeGreaterThanOrEqual(44);
        expect(width, `${selector} ширина`).toBeGreaterThanOrEqual(44);
      }
    }
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    expect(overflow).toBeLessThanOrEqual(0);
  });

  test('заголовок экрана: на телефоне в шапке, на Mac в шапке колонки с боковой панелью', async ({ page }, testInfo) => {
    await open(page);
    if (isMobile(testInfo)) {
      await expect(page.locator('.app-header h1')).toHaveText('Активность');
      await expect(page.locator('.desktop-sidebar')).toHaveCount(0);
      expect(await page.evaluate(() => window.innerWidth)).toBe(393);
    } else {
      await expect(page.locator('.desktop-thread-head h1')).toHaveText('Активность');
      await expect(page.locator('.desktop-sidebar')).toBeVisible();
      await expect(page.locator('#content')).toBeVisible();
    }
  });

  test('Mac: «Активность» в боковой панели ведёт на экран и подсвечена', async ({ page }, testInfo) => {
    test.skip(isMobile(testInfo), 'боковая панель только на Mac');
    await page.goto('/?mock=1#/');
    await page.locator('.desktop-sidebar').getByRole('link', { name: 'Активность' }).click();
    await expect(page).toHaveURL(/#\/activity$/);
    await expect(page.getByRole('heading', { level: 1, name: 'Активность' })).toBeVisible();
    await expect(page.locator('.desktop-sidebar').getByRole('link', { name: 'Активность' })).toHaveAttribute('aria-current', 'page');
  });

  test('чипы фильтров доступны с клавиатуры', async ({ page }) => {
    await open(page);
    const chip = kindFilter(page).getByRole('button', { name: 'Задачи' });
    await expect(chip).toHaveJSProperty('tagName', 'BUTTON');
    await chip.focus();
    await expect(chip).toBeFocused();
    await page.keyboard.press('Enter');
    await expect(chip).toHaveAttribute('aria-pressed', 'true');
    await expect(chip).toBeFocused();
    await page.keyboard.press('Enter');
    await expect(chip).toHaveAttribute('aria-pressed', 'false');
    await page.keyboard.press('Space');
    await expect(chip).toHaveAttribute('aria-pressed', 'true');
    await expect(chip).toBeFocused();  // лента перерисовывается, панель фильтров нет
    await expect(rows(page).first()).toBeVisible();
    await expect(chip).toBeFocused();
  });

  test('английский интерфейс: подписи переведены, имена ботов остаются', async ({ page }) => {
    await page.addInitScript(() => { try { localStorage.setItem('bothub.lang', 'en'); } catch { /* приватный режим */ } });
    await page.goto('/?mock=1&i18n=debug#/settings/activity');
    await expect(page.getByRole('heading', { level: 1, name: 'Activity' })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Activity log' })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Bot status' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Pause all' })).toBeVisible();
    await expect(page.getByRole('switch', { name: 'Pause bot Скаут' })).toBeVisible();
    await expect(page.getByRole('group', { name: 'Filter by bot' })).toBeVisible();
    await expect(rows(page).first()).toContainText('Approval requested');
    await expect(rows(page).first()).toContainText('SEND');
    await expect(page.getByRole('heading', { name: 'Today' })).toBeVisible();
    const missing = await page.evaluate(() => window.__i18nMissing || []);
    const mine = missing.filter((text) => /пауз|журнал|Пауза|Показать ещё|Сегодня|Вчера|Запрошено|Задача|Пропущено/.test(text));
    expect(mine).toEqual([]);
  });
});
