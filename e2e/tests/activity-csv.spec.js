import { expect, test } from '@playwright/test';

const ROUTE = '/?mock=1#/settings/activity';

test('кнопка «Скачать CSV» скачивает файл за выбранный период', async ({ page }) => {
  await page.goto(ROUTE);
  await expect(page.getByRole('heading', { level: 1, name: 'Активность' })).toBeVisible();
  await page.locator('#act-export-days').selectOption('30');
  const [download] = await Promise.all([
    page.waitForEvent('download'),
    page.getByRole('button', { name: 'Скачать CSV' }).click(),
  ]);
  expect(await download.suggestedFilename()).toBe('activity-30-days.csv');
  const text = await (await import('node:fs/promises')).readFile(await download.path(), 'utf8');
  expect(text.startsWith('﻿')).toBe(true);
  expect(text.split('\r\n')[0]).toBe('﻿time,bot,kind,code,title,detail');
});
