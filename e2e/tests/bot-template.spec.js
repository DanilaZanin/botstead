import { expect, test } from '@playwright/test';

// Шаблон бота (docs/contracts.md §9, этап 11): кнопка «Экспорт шаблона» в настройках бота скачивает
// <name>.botstead.json; кнопка «Создать из файла» на #/bots/new принимает файл, показывает имя/роль/
// число расписаний и процедур, гонит через ту же кнопку «Создать» с моделью из реестра.
// Мок-режим: ?mock=1. Вызовы экспорта и импорта: window.__botTemplateCalls {name, id, body}.

test.describe('экспорт и импорт шаблона бота', () => {
  test('экспорт из настроек бота: файл .botstead.json с шагами процедуры и расписанием', async ({ page }) => {
    await page.goto('/?mock=1#/bots/scout');
    // data-bot есть у многих элементов настроек: ждём конкретную кнопку экспорта
    await expect(page.getByRole('button', { name: 'Экспорт шаблона' })).toBeVisible();
    const download = page.waitForEvent('download');
    await page.getByRole('button', { name: 'Экспорт шаблона' }).click();
    const file = await download;
    expect(file.suggestedFilename()).toBe('Скаут.botstead.json');
    const doc = JSON.parse(await (await import('node:fs/promises')).readFile(await file.path(), 'utf8'));
    expect(doc.format).toBe('botstead-bot');
    expect(doc.version).toBe(1);
    expect(Object.keys(doc).sort()).toEqual([
      'auto_allow', 'auto_compact_percent', 'avatar', 'budget_daily_tokens',
      'executor', 'format', 'instructions', 'mcp_allow', 'name', 'procedures',
      'role', 'schedules', 'version',
    ]);
    expect(doc.name).toBe('Скаут');
    // Чужих полей нет
    for (const forbidden of ['id', 'owner_id', 'provider_id', 'model_id']) {
      expect(doc).not.toHaveProperty(forbidden);
    }
    const calls = await page.evaluate(() => window.__botTemplateCalls || []);
    expect(calls.filter((c) => c.name === 'export').at(-1)).toEqual({ name: 'export', id: 'scout' });
  });

  test('импорт из файла: показывает имя/роль/счётчики, создаёт нового бота', async ({ page }) => {
    await page.goto('/?mock=1#/bots/new');
    const fileInput = page.locator('#bn-file-input');
    await fileInput.setInputFiles({
      name: 'my-bot.botstead.json',
      mimeType: 'application/json',
      buffer: Buffer.from(JSON.stringify({
        format: 'botstead-bot', version: 1,
        name: 'Импортированный', role: 'Роль из файла', instructions: 'Инструкция',
        avatar: 'scout', executor: 'container',
        auto_allow: [{ tool: 'mac_find_files' }],
        mcp_allow: ['mcp__bothub__mac_find_files'],
        budget_daily_tokens: 250000, auto_compact_percent: 75,
        schedules: [{ cron: '0 9 * * 1-5', timezone: 'Europe/Moscow', prompt: 'проверь', enabled: true, name: 'утро' }],
        procedures: [{ format: 'bothub-procedure/1', name: 'Вход', description: '', params: [],
                       steps: [{ id: 's1', action: 'click', target: { role: 'button', name: 'Next' } }] }],
      })),
    });
    // Карточка с разобранным шаблоном
    await expect(page.getByText('Из шаблона', { exact: true })).toBeVisible();
    await expect(page.getByText('Импортированный · Роль из файла · расписаний: 1 · процедур: 1')).toBeVisible();
    // Имя редактируется, модель выбирается по умолчанию
    await expect(page.locator('#bn-name')).toHaveValue('Импортированный');
    await expect(page.locator('#bn-create')).toBeEnabled();
    await page.getByRole('button', { name: 'Создать', exact: true }).click();
    // После создания открывается тред нового бота
    await expect(page).toHaveURL(/#\/threads\/t-/);
    const calls = await page.evaluate(() => window.__botTemplateCalls || []);
    const lastImport = calls.filter((c) => c.name === 'import').at(-1);
    expect(lastImport.body.name).toBe('Импортированный');
    expect(lastImport.body.procedures).toHaveLength(1);
    expect(lastImport.body.schedules).toHaveLength(1);
  });

  test('импорт: занятое имя → « (2)»', async ({ page }) => {
    await page.goto('/?mock=1#/bots/new');
    await page.locator('#bn-file-input').setInputFiles({
      name: 'x.botstead.json',
      mimeType: 'application/json',
      buffer: Buffer.from(JSON.stringify({
        format: 'botstead-bot', version: 1, name: 'Скаут', role: 'р', instructions: 'и',
        avatar: 'scout', executor: 'container',
        auto_allow: [], mcp_allow: [], budget_daily_tokens: 200000, auto_compact_percent: 80,
        schedules: [], procedures: [{ format: 'bothub-procedure/1', name: 'Шаг', params: [],
                                      steps: [{ id: 's1', action: 'click', target: { role: 'button', name: 'Go' } }] }],
      })),
    });
    // Ждём карточку шаблона: до неё на странице кнопка «Создать из файла» (имя «Создать» совпадает по подстроке)
    await expect(page.locator('#bn-create')).toBeEnabled();
    await page.getByRole('button', { name: 'Создать', exact: true }).click();
    await expect(page).toHaveURL(/#\/threads\/t-/);
    const calls = await page.evaluate(() => window.__botTemplateCalls || []);
    const last = calls.filter((c) => c.name === 'import').at(-1);
    expect(last.body.name).toBe('Скаут');
    expect(last.created_name).toBe('Скаут (2)');
  });

  test('импорт: битый JSON или чужой формат не создаёт бота', async ({ page }) => {
    await page.goto('/?mock=1#/bots/new');
    await page.locator('#bn-file-input').setInputFiles({
      name: 'bad.botstead.json',
      mimeType: 'application/json',
      buffer: Buffer.from('{"format": "botstead-bot", "version": 1, "name": "Битый",'),
    });
    await expect(page.locator('#bn-file-error')).toContainText('Это не JSON');
    await expect(page.locator('#bn-from-file')).toBeVisible();
    await page.locator('#bn-file-input').setInputFiles({
      name: 'other.json',
      mimeType: 'application/json',
      buffer: Buffer.from(JSON.stringify({ format: 'other/1', version: 1, name: 'X' })),
    });
    await expect(page.locator('#bn-file-error')).toContainText('Файл не похож');
    const calls = await page.evaluate(() => window.__botTemplateCalls || []);
    expect(calls.filter((c) => c.name === 'import')).toHaveLength(0);
  });

  test('импорт: снятый переключатель расписания уходит в ядро пустым списком schedules', async ({ page }) => {
    await page.goto('/?mock=1#/bots/new');
    await page.locator('#bn-file-input').setInputFiles({
      name: 'sched.botstead.json',
      mimeType: 'application/json',
      buffer: Buffer.from(JSON.stringify({
        format: 'botstead-bot', version: 1, name: 'С расписанием', role: 'р',
        schedules: [{ cron: '0 9 * * 1-5', timezone: 'Europe/Moscow', prompt: 'проверь', enabled: true }],
      })),
    });
    await expect(page.locator('#bn-sched')).toBeChecked();
    await page.locator('#bn-sched').uncheck();
    await expect(page.locator('#bn-create')).toBeEnabled();
    await page.getByRole('button', { name: 'Создать', exact: true }).click();
    await expect(page).toHaveURL(/#\/threads\/t-/);
    const calls = await page.evaluate(() => window.__botTemplateCalls || []);
    expect(calls.filter((c) => c.name === 'import').at(-1).body.schedules).toEqual([]);
  });
});
