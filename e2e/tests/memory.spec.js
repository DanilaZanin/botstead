import { expect, test } from '@playwright/test';

// Тесты редактора памяти PWA: просмотр, фильтрация по ботам, поиск, ручное добавление,
// редактирование (сохранение введённого текста при ошибке), удаление с цитированием,
// принятие и отклонение предложений ботов, размеры сенсорных целей.

test.describe('редактор памяти PWA', () => {
  test('принимает предложенный ботом факт', async ({ page }) => {
    await page.goto('/?mock=1#/memory');
    await expect(page.getByRole('heading', { name: 'Память' })).toBeVisible();

    const proposedSection = page.getByRole('heading', { name: 'Предложено ботом' });
    await expect(proposedSection).toBeVisible();

    const proposedText = page.getByText(/Резюме EN для откликов/);
    await expect(proposedText).toBeVisible();

    // Нажимаем «Запомнить»
    await page.getByRole('button', { name: 'Запомнить' }).first().click();

    // Предложение исчезает из секции предложенных и остаётся в памяти
    await expect(page.locator('[data-action="remember-yes"]')).toHaveCount(0);
    await expect(proposedText).toBeVisible();
  });

  test('отклоняет предложенный ботом факт', async ({ page }) => {
    await page.goto('/?mock=1#/memory');
    await expect(page.getByRole('heading', { name: 'Предложено ботом' })).toBeVisible();

    const proposedText = page.getByText(/Резюме EN для откликов/);
    await expect(proposedText).toBeVisible();

    // Нажимаем «Не запоминать»
    await page.getByRole('button', { name: 'Не запоминать' }).first().click();

    // Предложение удаляется
    await expect(proposedText).toHaveCount(0);
  });

  test('добавляет новый факт вручную', async ({ page }) => {
    await page.goto('/?mock=1#/memory');

    // Открываем диалог добавления
    await page.locator('[data-action="add-memory"]').first().click();
    await expect(page.getByRole('heading', { name: 'Новая запись' })).toBeVisible();

    const testFact = 'Основной сервер: Ubuntu 24.04 в Белграде';
    await page.locator('#add-mem-text').fill(testFact);
    await page.locator('#add-mem-bot').selectOption({ label: 'Общая (для всех ботов)' });

    await page.locator('#add-mem-submit').click();

    // Диалог закрылся, факт появился в списке
    await expect(page.getByRole('heading', { name: 'Новая запись' })).toHaveCount(0);
    await expect(page.getByText(testFact)).toBeVisible();
  });

  test('ошибка сохранения при редактировании сохраняет введённый текст', async ({ page }) => {
    await page.goto('/?mock=1#/memory');

    // Открываем редактирование первой записи
    await page.locator('[data-action="edit-memory"]').first().click();
    await expect(page.getByRole('heading', { name: 'Редактирование записи' })).toBeVisible();

    // В режиме mock сети нет: мок отвечает 400, как ядро, на текст больше 16 КиБ.
    const tooLong = 'я'.repeat(9000);
    await page.locator('#edit-mem-text').fill(tooLong);
    await page.locator('#edit-mem-submit').click();

    // Диалог остаётся открытым, показана плашка ошибки, введённый текст не потерян
    await expect(page.getByRole('heading', { name: 'Редактирование записи' })).toBeVisible();
    await expect(page.locator('#edit-mem-alert')).toContainText('Текст не принят');
    await expect(page.locator('#edit-mem-text')).toHaveValue(tooLong);

    // Исправляем текст и повторяем сохранение
    const newText = 'Обновлённый текст, принятый со второй попытки';
    await page.locator('#edit-mem-text').fill(newText);
    await page.locator('#edit-mem-submit').click();

    // Диалог закрывается, обновлённый факт на экране
    await expect(page.getByRole('heading', { name: 'Редактирование записи' })).toHaveCount(0);
    await expect(page.getByText(newText)).toBeVisible();
  });

  test('удаляет запись с подтверждением и цитированием', async ({ page }) => {
    await page.goto('/?mock=1#/memory');

    const targetText = 'Релокация: сначала Сербия, потом Япония';
    await expect(page.getByText(targetText)).toBeVisible();

    // Нажимаем кнопку удаления у карточки
    const card = page.locator('.card', { hasText: targetText });
    await card.locator('[data-action="delete-memory"]').click();

    // Проверяем диалог подтверждения
    const dialog = page.getByRole('dialog');
    await expect(dialog).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Удаление записи' })).toBeVisible();
    await expect(dialog).toContainText(targetText);

    // Подтверждаем удаление
    await dialog.locator('[data-confirm]').click();

    // Диалог закрыт, запись удалена
    await expect(dialog).toHaveCount(0);
    await expect(page.getByText(targetText)).toHaveCount(0);
  });

  test('фильтрует записи по вкладкам ботов', async ({ page }) => {
    await page.goto('/?mock=1#/memory');

    const sharedText = 'Релокация: сначала Сербия, потом Япония';
    const sreText = 'server: диск 280 ГБ, Ubuntu, Docker';
    const macText = 'Договоры по квартире: ~/Documents/Квартира';

    // Вкладка «Общая»
    await page.getByRole('tab', { name: 'Общая' }).click();
    await expect(page.getByText(sharedText)).toBeVisible();
    await expect(page.getByText(sreText)).toHaveCount(0);
    await expect(page.getByText(macText)).toHaveCount(0);

    // Вкладка «SRE»
    await page.getByRole('tab', { name: 'SRE' }).click();
    await expect(page.getByText(sreText)).toBeVisible();
    await expect(page.getByText(sharedText)).toHaveCount(0);
    await expect(page.getByText(macText)).toHaveCount(0);

    // Вкладка «Все»
    await page.getByRole('tab', { name: 'Все' }).click();
    await expect(page.getByText(sharedText)).toBeVisible();
    await expect(page.getByText(sreText)).toBeVisible();
    await expect(page.getByText(macText)).toBeVisible();
  });

  test('ищет записи по поисковой строке', async ({ page }) => {
    await page.goto('/?mock=1#/memory');

    const searchInput = page.locator('#ms');
    await expect(searchInput).toBeVisible();

    // Поиск по слову «сертификаты»
    await searchInput.fill('сертификаты');
    await expect(page.getByText(/Сертификаты: certbot/)).toBeVisible();
    await expect(page.getByText(/server: диск 280 ГБ/)).toHaveCount(0);

    // Поиск по слову «квартира»
    await searchInput.fill('квартира');
    await expect(page.getByText(/Договоры по квартире/)).toBeVisible();
    await expect(page.getByText(/Сертификаты: certbot/)).toHaveCount(0);

    // Очистка поиска
    await searchInput.fill('');
    await expect(page.getByText(/Сертификаты: certbot/)).toBeVisible();
    await expect(page.getByText(/Договоры по квартире/)).toBeVisible();
  });

  test('сенсорные цели не меньше 44 px и вёрстка помещается в 393 px', async ({ page }) => {
    await page.goto('/?mock=1#/memory');

    // Проверяем высоту вкладок и кнопок
    const tabs = await page.locator('.mem-tab').evaluateAll((els) => els.map((el) => {
      const r = el.getBoundingClientRect();
      return { w: r.width, h: r.height };
    }));
    expect(tabs.filter((t) => t.h < 44 || t.w < 44)).toEqual([]);

    const iconBtns = await page.locator('[data-action="edit-memory"], [data-action="delete-memory"]').evaluateAll((els) => els.map((el) => {
      const r = el.getBoundingClientRect();
      return { w: r.width, h: r.height };
    }));
    expect(iconBtns.filter((b) => b.h < 44 || b.w < 44)).toEqual([]);

    const viewport = page.viewportSize();
    if (viewport && viewport.width <= 400) {
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
      expect(overflow).toBe(false);
    }
  });
});
