import { expect, test } from '@playwright/test';

// Экран расхода токенов и бюджета (docs/contracts.md §2, GET /api/usage/summary):
// переключатель периода (сегодня, 7 дней, 30 дней), график расхода по дням (div-столбцы с aria-label и скрытая таблица),
// дневной бюджет ботов с предупреждением от 80%, таблица по моделям, предохранитель, пустое состояние, ошибка и повтор.
// Мок-режим: ?mock=1. &usage=none|empty (нет данных), &usage=fail (первый запрос падает, повтор успешен), &usage=slow,
// &usage=xss (имя бота и модели с HTML). Журнал запросов мока: window.__usageCalls, записи {days}.
// Заголовок h1 «Расход» есть в обеих раскладках: .root-header на мобильной, .desktop-thread-head на десктопе.

const YOU = /(^|[^а-яё])(ты|тебя|тебе|тобой|твой|твоя|твоё|твои|твоих)(?![а-яё])|(Опиши|Выбери|Попробуй|Открой|Нажми|Введи|Смотри)(?![а-яё])/i;

async function openUsage(page, query = '') {
  await page.goto(`/?mock=1${query}#/usage`);
  await expect(page.getByRole('heading', { name: 'Расход', level: 1 })).toBeVisible({ timeout: 5000 });
}

test.describe('экран расхода токенов', () => {
  test('переключение периода: сегодня, 7 дней, 30 дней обновляет график и метку периода', async ({ page }) => {
    await openUsage(page);

    // По умолчанию выбран период «7 дней»
    const btnToday = page.getByRole('radio', { name: 'Период: Сегодня' });
    const btn7 = page.getByRole('radio', { name: 'Период: 7 дней' });
    const btn30 = page.getByRole('radio', { name: 'Период: 30 дней' });

    await expect(btn7).toBeVisible();
    await expect(btn7).toHaveAttribute('aria-checked', 'true');
    await expect(btnToday).toHaveAttribute('aria-checked', 'false');
    await expect(btn30).toHaveAttribute('aria-checked', 'false');

    await expect(page.getByText('Всего за период (за 7 дней)')).toBeVisible();
    const bars7 = page.locator('.usage-bar-col');
    await expect(bars7).toHaveCount(7);

    // Переключение на «Сегодня»
    await btnToday.click();
    await expect(btnToday).toHaveAttribute('aria-checked', 'true');
    await expect(btn7).toHaveAttribute('aria-checked', 'false');
    await expect(page.getByText('Всего за период (сегодня)')).toBeVisible();
    const bars1 = page.locator('.usage-bar-col');
    await expect(bars1).toHaveCount(1);
    await expect(page.locator('.usage-bar-label')).toHaveText('Сегодня');

    // Переключение на «30 дней»
    await btn30.click();
    await expect(btn30).toHaveAttribute('aria-checked', 'true');
    await expect(btnToday).toHaveAttribute('aria-checked', 'false');
    await expect(page.getByText('Всего за период (за 30 дней)')).toBeVisible();
    const bars30 = page.locator('.usage-bar-col');
    await expect(bars30).toHaveCount(30);
  });

  test('переключение периода меняет параметр days запроса', async ({ page }) => {
    await openUsage(page);
    const lastDays = () => page.evaluate(() => (window.__usageCalls || []).at(-1)?.days);
    const allDays = () => page.evaluate(() => (window.__usageCalls || []).map((c) => c.days));

    // Экран сам просит 7 дней и больше ничего не запрашивает
    await expect.poll(allDays).toEqual([7]);

    await page.getByRole('radio', { name: 'Период: Сегодня' }).click();
    await expect(page.getByText('Всего за период (сегодня)')).toBeVisible();
    expect(await lastDays()).toBe(1);

    await page.getByRole('radio', { name: 'Период: 30 дней' }).click();
    await expect(page.getByText('Всего за период (за 30 дней)')).toBeVisible();
    expect(await lastDays()).toBe(30);

    await page.getByRole('radio', { name: 'Период: 7 дней' }).click();
    await expect(page.getByText('Всего за период (за 7 дней)')).toBeVisible();
    expect(await allDays()).toEqual([7, 1, 30, 7]);
  });

  test('имя бота и модели с HTML показано текстом и не исполняется', async ({ page }) => {
    const evil = '<img src=x onerror="window.__usageXss=1">';
    await openUsage(page, '&usage=xss');

    // Бот: имя в строке бюджета текстом
    const botRow = page.locator('.usage-bot-row').filter({ hasText: evil });
    await expect(botRow).toHaveCount(1);
    await expect(botRow.locator('img[src="x"]')).toHaveCount(0);

    // Модель: имя в таблице текстом
    const modelRow = page.locator('.usage-table tbody tr').filter({ hasText: evil });
    await expect(modelRow).toHaveCount(1);

    // Ни одного вставленного img, обработчик не сработал (даём onerror время, если бы тег был живой)
    await expect(page.locator('#app img[src="x"]')).toHaveCount(0);
    await page.waitForTimeout(300);
    expect(await page.evaluate(() => window.__usageXss)).toBeUndefined();
  });

  test('столбцы графика имеют доступные подписи aria-label и дублируются таблицей для скринридеров', async ({ page }) => {
    await openUsage(page);

    const bars = page.locator('.usage-bar');
    await expect(bars).toHaveCount(7);

    // Каждый столбец имеет роль img, tabindex="0" и непустой aria-label с датой и токенами
    const count = await bars.count();
    for (let i = 0; i < count; i++) {
      const bar = bars.nth(i);
      await expect(bar).toHaveAttribute('role', 'img');
      await expect(bar).toHaveAttribute('tabindex', '0');
      const label = await bar.getAttribute('aria-label');
      expect(label).toBeTruthy();
      expect(label).toMatch(/\d{1,2}\s+[а-я]+\s+\d{4}:\s+[\d\u202F]+\s+токенов/);
    }

    // Скрытая таблица для скринридеров
    const hiddenTable = page.locator('.usage-chart-wrap table.sr-only');
    await expect(hiddenTable).toBeAttached();
    await expect(hiddenTable.locator('caption')).toHaveText('Расход токенов по дням');
    await expect(hiddenTable.locator('th')).toHaveText(['Дата', 'Токены']);
    const rows = hiddenTable.locator('tbody tr');
    await expect(rows).toHaveCount(7);
  });

  test('список ботов: дневной бюджет, шкала расхода и предупреждение при расходе от 80%', async ({ page }) => {
    await openUsage(page);

    await expect(page.getByText('Дневной бюджет ботов')).toBeVisible();

    // Бот Кодер израсходовал 100% бюджета (200 000 из 200 000) - стиль опасности/предупреждения
    const coderRow = page.locator('.usage-bot-row', { hasText: 'Кодер' });
    await expect(coderRow).toBeVisible();
    await expect(coderRow.locator('.usage-danger, .usage-warn')).toContainText('100%');
    await expect(coderRow.locator('.meter-fill-danger')).toBeVisible();

    // Скаут (128 000 из 200 000 = 64%)
    const scoutRow = page.locator('.usage-bot-row', { hasText: 'Скаут' });
    await expect(scoutRow).toBeVisible();
    await expect(scoutRow).toContainText('64%');
    await expect(scoutRow.locator('.meter-fill-danger')).toHaveCount(0);
    await expect(scoutRow.locator('.meter-fill-warn')).toHaveCount(0);

    // Проверка доступности элементов meter
    const meters = page.locator('.usage-bot-row [role="meter"]');
    await expect(meters).toHaveCount(5);
    for (let i = 0; i < 5; i++) {
      const meter = meters.nth(i);
      await expect(meter).toHaveAttribute('aria-valuemin', '0');
      await expect(meter).toHaveAttribute('aria-valuemax', '100');
      const val = await meter.getAttribute('aria-valuenow');
      expect(Number(val)).toBeGreaterThanOrEqual(0);
      expect(Number(val)).toBeLessThanOrEqual(100);
    }
  });

  test('таблица расхода по моделям: входящие, исходящие, кэш, сумма и ходы', async ({ page }) => {
    await openUsage(page);

    await expect(page.getByText('Расход по моделям')).toBeVisible();
    const table = page.locator('.usage-table');
    await expect(table).toBeVisible();

    const headers = table.locator('th');
    await expect(headers).toHaveText(['Модель', 'Вход', 'Выход', 'Кэш чтение', 'Кэш запись', 'Итого', 'Ходы']);

    await expect(table.getByText('claude-sonnet-5')).toBeVisible();
    await expect(table.getByText('claude-opus-5-5')).toBeVisible();
    await expect(table.getByText('gpt-5.4')).toBeVisible();
    await expect(table.getByText('gemini-3.1-pro-preview')).toBeVisible();
  });

  test('карточка предохранителя при сбое бота', async ({ page }) => {
    await openUsage(page);

    await expect(page.getByText(/остановлен предохранителем/)).toBeVisible();
    await expect(page.getByText('npm ERR! ERESOLVE unable to resolve dependency tree')).toBeVisible();
    await expect(page.getByRole('link', { name: 'Открыть тред' })).toBeVisible();
    await expect(page.getByRole('link', { name: 'Другая модель' })).toBeVisible();
  });

  test('пустое состояние: понятный экран без битых элементов', async ({ page }) => {
    await openUsage(page, '&usage=none');

    await expect(page.getByRole('heading', { name: 'Расхода пока нет' })).toBeVisible();
    await expect(page.getByText(/Когда боты начнут выполнять задачи/)).toBeVisible();
    await expect(page.locator('.usage-chart')).toHaveCount(0);
    await expect(page.locator('.usage-table')).toHaveCount(0);
  });

  test('состояние ошибки и кнопка повторить', async ({ page }) => {
    await page.goto('/?mock=1&usage=fail&x=1#/usage');

    await expect(page.getByRole('alert')).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Не получилось загрузить расход' })).toBeVisible();

    const retry = page.getByRole('button', { name: 'Повторить' });
    await expect(retry).toBeVisible();
    await retry.click();

    await expect(page.getByText('Всего за период')).toBeVisible();
    await expect(page.locator('.usage-chart')).toBeVisible();
  });

  test('размеры touch-элементов не меньше 44 px и нет горизонтальной прокрутки на 393 px', async ({ page }) => {
    await openUsage(page);

    const interactive = page.locator('.usage-period-btn, .usage-chart-card, .btn');
    const count = await interactive.count();
    for (let i = 0; i < count; i++) {
      const el = interactive.nth(i);
      if (await el.isVisible()) {
        const box = await el.boundingBox();
        if (box) {
          expect(box.height).toBeGreaterThanOrEqual(44);
        }
      }
    }

    const scrollWidth = await page.evaluate(() => document.documentElement.scrollWidth);
    const innerWidth = await page.evaluate(() => window.innerWidth);
    expect(scrollWidth).toBeLessThanOrEqual(innerWidth);
  });

  test('отсутствие обращений на «ты» и отсутствие длинного тире в тексте экрана', async ({ page }) => {
    await openUsage(page);

    const appText = await page.locator('#app').innerText();
    expect(appText.match(YOU)?.[0]).toBeUndefined();
    expect(appText).not.toContain('—');

    await page.goto('/?mock=1&usage=none#/usage');
    await expect(page.getByRole('heading', { name: 'Расхода пока нет' })).toBeVisible();
    const emptyText = await page.locator('#app').innerText();
    expect(emptyText.match(YOU)?.[0]).toBeUndefined();
    expect(emptyText).not.toContain('—');
  });
});
