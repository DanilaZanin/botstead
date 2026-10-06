import { expect, test } from '@playwright/test';

// Поле «Разрешённые MCP» в настройках бота (mcp_allow): одно имя на строку, пустые строки и повторы отбрасываются,
// сохранение кнопкой, несохранённый текст не затирается другими переключателями. Мок-бэкенд: pwa/api.js (?mock=1),
// состояние бота читается через window.__ctxMock.bot. Тот же файл идёт в обоих проектах Playwright: 393 px и 1280 px.

const open = (page) => page.goto('/?mock=1#/bots/scout');
const field = (page) => page.locator('#mcp-allow');
const save = (page) => page.getByRole('button', { name: 'Сохранить список' });
const saved = (page) => page.evaluate(() => window.__ctxMock.bot('scout').mcp_allow);

test.describe('разрешённые MCP в настройках бота', () => {
  test('карточка с подсказкой, по умолчанию список пуст', async ({ page }) => {
    await open(page);
    await expect(page.getByText('Разрешённые MCP', { exact: true })).toBeVisible();
    await expect(field(page)).toBeVisible();
    await expect(field(page)).toHaveValue('');
    await expect(field(page)).toHaveAccessibleName('Разрешённые MCP');
    await expect(page.locator('#mcp-allow-hint')).toContainText('Пусто: у бота только инструменты bothub');
    await expect(field(page)).toHaveAttribute('aria-describedby', 'mcp-allow-hint mcp-allow-executor-hint');
    await expect(page.locator('#mcp-allow-executor-hint')).toContainText('Gemini (agy) список не применяет');  // scout в моке на Gemini
  });

  test('подсказка по исполнителю: Claude, Codex, Gemini', async ({ page }) => {
    for (const [bot, text] of [['sre', 'Claude: инструмент вне списка отклоняется сразу'], ['coder', 'Codex: вызов инструмента вне списка останавливает ход'],
      ['scout', 'Gemini (agy) список не применяет']]) {
      await page.goto(`/?mock=1#/bots/${bot}`);
      await expect(page.locator('#mcp-allow-executor-hint')).toContainText(text);
    }
    await expect(save(page)).toBeVisible();
  });

  test('сохраняет список без пустых строк и повторов, после перезахода он на месте', async ({ page }) => {
    await open(page);
    await field(page).fill('  mcp__github__*  \n\nslack\nmcp__github__*\n   \ngithub.create_issue');
    await save(page).click();
    await expect.poll(() => saved(page)).toEqual(['mcp__github__*', 'slack', 'github.create_issue']);
    await expect(page.locator('#bot-alert [role="alert"]')).toHaveCount(0);
    await expect(field(page)).toHaveValue('mcp__github__*\nslack\ngithub.create_issue');

    await page.evaluate(() => { location.hash = '#/'; });
    await expect(field(page)).toHaveCount(0);
    await page.evaluate(() => { location.hash = '#/bots/scout'; });
    await expect(field(page)).toHaveValue('mcp__github__*\nslack\ngithub.create_issue');
  });

  test('пустое поле очищает список', async ({ page }) => {
    await open(page);
    await field(page).fill('slack');
    await save(page).click();
    await expect.poll(() => saved(page)).toEqual(['slack']);
    await field(page).fill('');
    await save(page).click();
    await expect.poll(() => saved(page)).toEqual([]);
  });

  test('несохранённый текст не затирается другим переключателем', async ({ page }) => {
    await open(page);
    await field(page).fill('mcp__github__create_issue');
    await page.locator('#ac-switch').click();
    await expect.poll(() => page.evaluate(() => window.__ctxMock.bot('scout').auto_compact_percent)).toBe(null);
    await expect(field(page)).toHaveValue('mcp__github__create_issue');
    expect(await saved(page) ?? []).toEqual([]);
  });

  test('слишком длинное имя не уходит на сервер, текст остаётся', async ({ page }) => {
    await open(page);
    const long = 'x'.repeat(201);
    await field(page).fill(long);
    await save(page).click();
    await expect(page.locator('#bot-alert [role="alert"]')).toContainText('Изменение не сохранено');
    await expect(page.locator('#bot-alert [role="alert"]')).toContainText('до 200 символов');
    await expect(field(page)).toHaveValue(long);
    expect(await saved(page) ?? []).toEqual([]);
  });

  test('больше 200 строк не уходит на сервер', async ({ page }) => {
    await open(page);
    await field(page).fill(Array.from({ length: 201 }, (_, i) => `srv${i}`).join('\n'));
    await save(page).click();
    await expect(page.locator('#bot-alert [role="alert"]')).toContainText('Не больше 200 строк');
    expect(await saved(page) ?? []).toEqual([]);
  });

  test('ровно 200 строк по 200 символов сохраняются', async ({ page }) => {
    await open(page);
    await field(page).fill(Array.from({ length: 200 }, (_, i) => String(i).padStart(200, 'a')).join('\n'));
    await save(page).click();
    await expect.poll(async () => (await saved(page))?.length ?? 0).toBe(200); // до сохранения поля нет: опрос не должен падать
    await expect(page.locator('#bot-alert [role="alert"]')).toHaveCount(0);
  });
});
