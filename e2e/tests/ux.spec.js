import { expect, test } from '@playwright/test';

// Правки по UX-оценке живой вёрстки: по сценарию на каждый пункт, где поведение изменилось.
// Мок-режим: ?mock=1 (по умолчанию вошёл админ), &role=member, &auth=none, &setup=1.

const ADMIN = 'admin@example.org';
const MOCK_IPHONE_UA = 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1';
const isMobile = (testInfo) => testInfo.project.name === 'mobile-chromium';

// Подписи, которые не должны обрезаться многоточием: у элемента нет скрытого переполнения.
async function truncatedTexts(locator) {
  return locator.evaluateAll((rows) => rows.flatMap((row) => Array.from(row.querySelectorAll('.row-title, .row-sub'))
    .filter((el) => el.scrollWidth > el.clientWidth + 1)
    .map((el) => el.textContent.trim())));
}

async function acceptInvite(page) {
  await page.goto('/?mock=1&auth=none#/invite/valid-token');
  await page.getByLabel('Email', { exact: true }).fill('new@example.org');
  await page.getByLabel('Пароль', { exact: true }).fill('long-enough-pass');
  await page.getByLabel('Пароль ещё раз', { exact: true }).fill('long-enough-pass');
  await page.getByRole('button', { name: 'Создать аккаунт' }).click();
  await expect(page.getByRole('heading', { name: 'Аккаунт создан' })).toBeVisible();
}

test.describe('1. приветствие после регистрации', () => {
  test('главное действие «Создать бота» активно и ведёт к созданию бота', async ({ page }, testInfo) => {
    await acceptInvite(page);
    const create = page.getByRole('link', { name: 'Создать бота' });
    await expect(create).toBeVisible();
    if (isMobile(testInfo)) {
      const box = await create.boundingBox();
      expect(box.height).toBeGreaterThanOrEqual(44);
      expect(box.y + box.height).toBeGreaterThan(852 - 120); // главное действие внизу экрана
    }
    await create.click();
    await expect(page).toHaveURL(/#\/bots\/new$/);
    await expect(page.getByLabel('Что бот должен делать и как работать')).toBeVisible();
  });

  test('обновление страницы и прямой заход давнего пользователя ведут к списку ботов', async ({ page }) => {
    await acceptInvite(page);
    await page.reload();
    await expect(page).toHaveURL(/#\/$/);
    await expect(page.locator('[data-action="open-thread"][data-bot="scout"]').first()).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Аккаунт создан' })).toHaveCount(0);

    await page.goto('/?mock=1#/welcome');
    await expect(page).toHaveURL(/#\/$/);
    await expect(page.locator('[data-action="open-thread"][data-bot="scout"]').first()).toBeVisible();
  });
});

test.describe('3. подтверждение опасных действий', () => {
  test('отзыв инвайта: лист на телефоне, диалог на десктопе, «Отмена» ничего не меняет', async ({ page }, testInfo) => {
    await page.goto('/?mock=1#/settings/users');
    await expect(page.getByRole('heading', { name: /Инвайты · 2/ })).toBeVisible();
    await page.getByRole('button', { name: /^Отозвать инвайт/ }).first().click();
    const dialog = page.getByRole('dialog', { name: 'Отозвать инвайт?' });
    await expect(dialog).toContainText('Ссылка перестанет работать');
    const box = await dialog.boundingBox();
    const viewport = page.viewportSize();
    if (isMobile(testInfo)) {
      expect(Math.round(box.y + box.height)).toBe(viewport.height); // лист снизу
    } else {
      expect(box.y).toBeGreaterThan(40); // окно по центру
      expect(box.y + box.height).toBeLessThan(viewport.height - 40);
    }
    await expect(dialog.getByRole('button', { name: 'Отмена' })).toBeFocused(); // фокус не на опасной кнопке
    await dialog.getByRole('button', { name: 'Отмена' }).click();
    await expect(page.getByRole('dialog')).toHaveCount(0);
    await expect(page.getByRole('heading', { name: /Инвайты · 2/ })).toBeVisible();
  });
});

test.describe('4. строки инвайтов', () => {
  test('заголовок без дубля роли, срок окончания полностью, ничего не обрезано', async ({ page }) => {
    await page.goto('/?mock=1#/settings/users');
    const rows = page.locator('.invite-row');
    await expect(rows).toHaveCount(2);

    const active = rows.nth(0);
    await expect(active.locator('.row-title')).toHaveText('Инвайт без заметки');
    await expect(active.locator('.badge')).toHaveText('участник');
    await expect(active.locator('.row-sub').nth(0)).toHaveText(/^Действует до \d+ \S+, \d{2}:\d{2}$/);
    await expect(active.locator('.row-sub').nth(1)).toHaveText(/^Осталось 4[56] ч$/);

    const expired = rows.nth(1);
    await expect(expired.locator('.row-title')).toHaveText('Инвайт без заметки');
    await expect(expired.locator('.row-sub')).toHaveText(/^Истёк \d+ \S+, \d{2}:\d{2}$/);

    expect(await truncatedTexts(rows)).toEqual([]);
  });

  test('инвайт с заметкой: «Инвайт: заметка» и отдельный бейдж роли', async ({ page }) => {
    await page.goto('/?mock=1#/settings/users');
    await page.getByRole('button', { name: 'Создать инвайт' }).click();
    const dialog = page.getByRole('dialog', { name: 'Новый инвайт' });
    await dialog.getByLabel('Для кого').fill('Миша');
    await dialog.getByRole('radio', { name: 'Админ' }).click();
    await dialog.getByRole('button', { name: 'Создать ссылку' }).click();
    const done = page.getByRole('dialog', { name: 'Ссылка готова' });
    await expect(done).toContainText('Инвайт: Миша · админ');
    await done.getByRole('button', { name: 'Закрыть' }).first().click();
    const row = page.locator('.invite-row').filter({ hasText: 'Миша' });
    await expect(row.locator('.row-title')).toHaveText('Инвайт: Миша');
    await expect(row.locator('.badge')).toHaveText('админ');
    expect(await truncatedTexts(page.locator('.invite-row'))).toEqual([]);
  });
});

test.describe('5. мои сессии', () => {
  test('срок не обрезается, иконка по user_agent, без совпадения «Это устройство» не ставится', async ({ page }) => {
    await page.goto('/?mock=1#/settings/sessions');
    const rows = page.locator('.list-row');
    await expect(rows).toHaveCount(2);
    await expect(rows.nth(0).locator('.row-sub').nth(1)).toHaveText(/^Действует до \d+ \S+$/);
    expect(await truncatedTexts(rows)).toEqual([]);
    await expect(page.locator('.list-row', { hasText: 'iPhone · Safari' }).locator('.row-icon')).toHaveAttribute('data-device', 'phone');
    await expect(page.locator('.list-row', { hasText: 'Mac · Chrome' }).locator('.row-icon')).toHaveAttribute('data-device', 'desktop');
    // user_agent браузера теста не совпал ни с одной сессией: ничего не помечаем
    await expect(page.getByText('Это устройство')).toHaveCount(0);
  });

  test('правило выбора текущей сессии: поле current главнее, затем единственное совпадение user_agent', async ({ page }) => {
    await page.goto('/?mock=1#/settings/sessions');
    await expect(page.locator('.list-row')).toHaveCount(2);
    const result = await page.evaluate(async () => {
      const { currentSessionId } = await import('/account.js');
      const ua = navigator.userAgent;
      return {
        flag: currentSessionId([{ id_hash: 'a', current: false, user_agent: ua }, { id_hash: 'b', current: true, user_agent: 'x' }]),
        flagNone: currentSessionId([{ id_hash: 'a', current: false, user_agent: ua }]),
        single: currentSessionId([{ id_hash: 'a', user_agent: ua }, { id_hash: 'b', user_agent: 'другой' }]),
        twins: currentSessionId([{ id_hash: 'a', user_agent: ua }, { id_hash: 'b', user_agent: ua }]),
        none: currentSessionId([{ id_hash: 'a', user_agent: 'другой' }]),
      };
    });
    expect(result).toEqual({ flag: 'b', flagNone: null, single: 'a', twins: null, none: null });
  });

  test.describe('браузер совпал с user_agent сессии', () => {
    test.use({ userAgent: MOCK_IPHONE_UA });

    test('сессия помечена «Это устройство», выход из неё называется честно', async ({ page }) => {
      await page.goto('/?mock=1#/settings/sessions');
      const here = page.locator('.list-row', { hasText: 'iPhone · Safari' });
      await expect(here.getByText('Это устройство')).toBeVisible();
      await expect(page.getByText('Это устройство')).toHaveCount(1);
      await here.getByRole('button', { name: /Завершить сессию/ }).click();
      await expect(page.getByRole('dialog', { name: 'Выйти на этом устройстве?' })).toBeVisible();
    });
  });
});

test.describe('7. путь к настройкам бота', () => {
  test('телефон: иконка настроек в карточке бота, назад возвращает в список', async ({ page }, testInfo) => {
    test.skip(!isMobile(testInfo), 'карточки ботов только на телефоне');
    await page.goto('/?mock=1');
    const settings = page.getByRole('link', { name: 'Настройки бота: Скаут' });
    await expect(settings).toBeVisible();
    const box = await settings.boundingBox();
    expect(box.width).toBeGreaterThanOrEqual(44);
    expect(box.height).toBeGreaterThanOrEqual(44);
    await settings.click();
    await expect(page).toHaveURL(/#\/bots\/scout$/);
    await page.getByRole('link', { name: 'Назад' }).click();
    await expect(page).toHaveURL(/#\/$/);
    await expect(page.getByRole('link', { name: 'Настройки бота: Скаут' })).toBeVisible();
  });

  test('телефон: в шапке треда ползунки, назад из настроек возвращает в тред', async ({ page }, testInfo) => {
    test.skip(!isMobile(testInfo), 'шапка треда с иконками только на телефоне');
    await page.goto('/?mock=1');
    await page.locator('[data-action="open-thread"][data-bot="scout"]').first().click();
    await expect(page).toHaveURL(/#\/threads\/t-scout$/);
    const icon = page.getByRole('link', { name: 'Настройки бота', exact: true });
    await expect(icon).toHaveAttribute('data-icon', 'sliders');
    await expect(icon.locator('svg circle')).toHaveCount(2); // ползунки, а не шестерёнка с лучами
    await icon.click();
    await expect(page.getByText('Настройки бота', { exact: true })).toBeVisible();
    await page.getByRole('link', { name: 'Назад' }).click();
    await expect(page).toHaveURL(/#\/threads\/t-scout$/);
    await expect(page.locator('#composer-input')).toBeVisible();
  });

  test('десктоп: из треда в настройки и «К треду» обратно', async ({ page }, testInfo) => {
    test.skip(isMobile(testInfo), 'кнопки в шапке только на десктопе');
    await page.goto('/?mock=1#/threads/t-scout');
    await page.getByRole('link', { name: 'Настройки бота', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Скаут' })).toBeVisible();
    await page.getByRole('link', { name: 'К треду' }).click();
    await expect(page).toHaveURL(/#\/threads\/t-scout$/);
  });

  test('у строки пользователя иконка действий «…»', async ({ page }) => {
    await page.goto('/?mock=1#/settings/users');
    const action = page.getByRole('button', { name: 'Действия: alice@example.org' });
    await expect(action.locator('svg circle')).toHaveCount(3);
  });
});

test.describe('6. настройки бота', () => {
  test('подписи «Модель» и «Без вопроса», нигде не осталось «Мозг» и «Без спроса»', async ({ page }) => {
    await page.goto('/?mock=1#/bots/scout');
    await expect(page.getByText('Модель', { exact: true })).toBeVisible();
    await expect(page.getByText('Без вопроса: чтение файлов, поиск, скриншот')).toBeVisible();
    for (const hash of ['#/bots/scout', '#/approvals', '#/bots/new']) {
      await page.goto(`/?mock=1${hash}`);
      await expect(page.locator('#app *').first()).toBeVisible();
      await page.waitForTimeout(300);
      const text = await page.locator('body').innerText();
      expect(text).not.toMatch(/Мозг|Без спроса|без спроса/);
    }
  });

  test('аватары и сегменты не меньше 44 px', async ({ page }) => {
    await page.goto('/?mock=1#/bots/scout');
    await expect(page.getByText('Модель', { exact: true })).toBeVisible();
    const sizes = await page.locator('.avatar-pick, .segmented button').evaluateAll((els) => els.map((el) => {
      const r = el.getBoundingClientRect();
      return { label: el.getAttribute('aria-label') || el.textContent.trim(), w: Math.round(r.width), h: Math.round(r.height) };
    }));
    expect(sizes.filter((s) => s.label.startsWith('Персонаж:'))).toHaveLength(10);
    expect(sizes.length).toBeGreaterThanOrEqual(10 + 2);
    expect(sizes.filter((s) => s.w < 44 || s.h < 44)).toEqual([]);
  });

  test('смена модели обновляет подпись и точку на аватаре', async ({ page }, testInfo) => {
    await page.goto('/?mock=1#/bots/scout');
    await expect(page.locator('[data-model-label]')).toHaveText('Gemini 3.1 Pro');
    if (isMobile(testInfo)) await page.locator('[data-pick-open]').click(); // на телефоне выбор модели в листе
    await page.locator('[role="radio"][data-provider="p-claude"]').filter({ hasText: 'Sonnet 5' }).click();
    await expect(page.locator('[data-model-label]')).toHaveText('Sonnet 5');
    const slot = await page.locator('[data-avatar-slot="scout"]').first().innerHTML();
    expect(slot).toContain('--claude-fg');
    expect(slot).toContain('<img src="./avatars/scout.webp"');
  });

  test('экран не перерисовывается целиком: узел тот же, фокус остаётся на элементе', async ({ page }) => {
    await page.goto('/?mock=1#/bots/scout');
    await expect(page.getByText('Модель', { exact: true })).toBeVisible();
    const targets = [
      page.getByRole('radio', { name: 'Mac', exact: true }),
      page.getByRole('button', { name: 'Персонаж: Сова' }),
      page.getByRole('switch', { name: /Полный контроль Mac/ }),
    ];
    for (const target of targets) {
      await target.evaluate((el) => { window.__target = el; });
      await target.click();
      // дожидаемся ответа мока: подпись/состояние уже обновились
      await page.waitForTimeout(400);
      const same = await page.evaluate(() => document.contains(window.__target) && document.activeElement === window.__target);
      expect(same, 'тот же узел в фокусе').toBe(true);
    }
    await expect(page.getByRole('radio', { name: 'Mac', exact: true })).toHaveAttribute('aria-checked', 'true');
    await expect(page.getByRole('button', { name: 'Персонаж: Сова' })).toHaveAttribute('aria-pressed', 'true');
  });
});

test.describe('8. лист «Новый инвайт»', () => {
  test('на телефоне без автофокуса на поле «Для кого», на десктопе поле в фокусе', async ({ page }, testInfo) => {
    await page.goto('/?mock=1#/settings/users');
    await page.getByRole('button', { name: 'Создать инвайт' }).click();
    const dialog = page.getByRole('dialog', { name: 'Новый инвайт' });
    await expect(dialog).toBeVisible();
    if (isMobile(testInfo)) {
      await expect(dialog).toBeFocused();
      await expect(dialog.getByLabel('Для кого')).not.toBeFocused();
    } else {
      await expect(dialog.getByLabel('Для кого')).toBeFocused();
    }
  });
});

test.describe('9. вход: ошибки и фокус', () => {
  async function openLogin(page) {
    await page.goto('/?mock=1&auth=none');
    await expect(page.getByRole('heading', { name: 'botstead' })).toBeVisible();
  }

  test('500: человеческий текст без кода ответа, фокус на сообщении', async ({ page }) => {
    await openLogin(page);
    await page.getByLabel('Email', { exact: true }).fill(ADMIN);
    await page.getByLabel('Пароль', { exact: true }).fill('server-down');
    await page.getByRole('button', { name: 'Войти', exact: true }).click();
    const message = page.locator('#login-alert [role="alert"]');
    await expect(message).toContainText('Сервер не отвечает');
    await expect(message).toContainText('Попробуйте ещё раз через минуту');
    await expect(page.locator('#login-alert')).not.toContainText(/Ответ|500/);
    await expect(message).toBeFocused();
  });

  test('ошибка смены пароля: фокус на сообщении, email подставлен после успеха', async ({ page }) => {
    await page.goto('/?mock=1#/settings/password');
    await page.getByLabel('Текущий пароль', { exact: true }).fill('server-down');
    await page.getByLabel('Новый пароль', { exact: true }).fill('new-password-456');
    await page.getByLabel('Новый пароль ещё раз', { exact: true }).fill('new-password-456');
    await page.getByRole('button', { name: 'Сменить пароль' }).click();
    const message = page.locator('#pw-alert [role="alert"]');
    await expect(message).toContainText('Сервер не отвечает');
    await expect(message).not.toContainText(/Ответ|500/);
    await expect(message).toBeFocused();
    await expect(message).toHaveAttribute('tabindex', '-1');

    await page.getByLabel('Текущий пароль', { exact: true }).fill('too-many-attempts');
    await page.getByRole('button', { name: 'Сменить пароль' }).click();
    await expect(message).toContainText('Слишком много попыток');
    await expect(message).toBeFocused();
  });
});

test.describe('10. безличные формулировки', () => {
  const YOU = /(^|[^а-яё])(ты|тебя|тебе|тобой|твой|твоя|твоё|твои|твоих)(?![а-яё])|(Опиши|Выбери|Попробуй|Открой|Нажми|Введи|Смотри)(?![а-яё])/i;

  test('в интерфейсе нет обращения на «ты»', async ({ page }, testInfo) => {
    const routes = ['#/', '#/bots/new', '#/bots/scout', '#/memory', '#/settings', '#/routines', '#/approvals', '#/usage', '#/threads/t-scout'];
    if (isMobile(testInfo)) routes.push('#/threads/t-scout/handoff');
    for (const hash of routes) {
      await page.goto(`/?mock=1&x=${encodeURIComponent(hash)}${hash}`);
      await expect.poll(async () => (await page.locator('#app').innerText()).length, { message: hash }).toBeGreaterThan(30);
      await page.waitForTimeout(300);
      const text = await page.locator('#app').innerText();
      const attrs = await page.locator('#app [placeholder], #app [aria-label]').evaluateAll((els) => els.map((el) => `${el.getAttribute('placeholder') || ''} ${el.getAttribute('aria-label') || ''}`));
      const found = [text, ...attrs].map((t) => t.match(YOU)?.[0]).filter(Boolean);
      expect(found, `${hash}: ${found.join(', ')}`).toEqual([]);
    }
    await page.goto('/?mock=1');
    await expect(page.getByText('Ждёт решения').first()).toBeVisible();
  });
});

test.describe('12. десктоп: навигация с клавиатуры и история', () => {
  test('«К содержимому» первая по Tab и переводит фокус в основную область', async ({ page }, testInfo) => {
    test.skip(isMobile(testInfo), 'ссылка только в десктопной раскладке');
    for (const hash of ['#/', '#/settings']) {
      await page.goto(`/?mock=1&r=${encodeURIComponent(hash)}${hash}`);
      await expect(page.locator('.desktop-main')).toBeVisible();
      await page.keyboard.press('Tab');
      const link = page.getByRole('link', { name: 'К содержимому' });
      await expect(link).toBeFocused();
      await expect(link).toBeVisible();
      await page.keyboard.press('Enter');
      await expect(page.locator('#content')).toBeFocused();
      await expect(page).toHaveURL(new RegExp(`${hash}$`));
    }
  });

  test('после выхода «Назад» остаётся в приложении: запись истории заменена', async ({ page }) => {
    await page.goto('/?mock=1');
    await page.getByRole('link', { name: 'Настройки', exact: true }).click();
    await expect(page).toHaveURL(/#\/settings$/);
    const before = await page.evaluate(() => history.length);
    await page.getByRole('button', { name: 'Выйти' }).click();
    await expect(page.getByRole('heading', { name: 'botstead' })).toBeVisible();
    expect(await page.evaluate(() => history.length)).toBe(before);
    await expect(page).toHaveURL(/#\/$/);
    await page.goBack();
    await expect(page.getByRole('heading', { name: 'botstead' })).toBeVisible();
    expect(await page.evaluate(() => document.getElementById('app').children.length)).toBeGreaterThan(0);
  });
});

// ---------------------------------------------------------------------------
// Правки по второй UX-оценке (провайдеры и терминал входа): приветствие, мастер, согласованность, лист модели
// ---------------------------------------------------------------------------
// Выбор модели: на телефоне лист по кнопке, на Mac блок прямо в карточке.
async function openPicker(page, testInfo) {
  if (isMobile(testInfo)) {
    await page.locator('[data-pick-open]').click();
    await expect(page.getByRole('dialog', { name: 'Модель бота' })).toBeVisible();
  }
}

test.describe('приветствие: сбой проверки моделей', () => {
  test('«Не удалось проверить модели» с повтором, а не «моделей нет»', async ({ page }) => {
    await page.goto('/?mock=1&auth=none&providers=fail#/invite/valid-token');
    await page.getByLabel('Email', { exact: true }).fill('new@example.org');
    await page.getByLabel('Пароль', { exact: true }).fill('long-enough-pass');
    await page.getByLabel('Пароль ещё раз', { exact: true }).fill('long-enough-pass');
    await page.getByRole('button', { name: 'Создать аккаунт' }).click();
    await expect(page.getByRole('heading', { name: 'Аккаунт создан' })).toBeVisible();
    await expect(page.getByText('Не удалось проверить модели').first()).toBeVisible();
    await expect(page.getByText('Станет доступно, когда будет хотя бы одна включённая модель')).toHaveCount(0);
    await expect(page.getByRole('link', { name: 'Подключить модель' })).toHaveCount(0);
    await page.getByRole('button', { name: 'Повторить проверку' }).click();
    await expect(page.getByRole('link', { name: 'Создать бота' })).toBeEnabled();
    await expect(page.getByText('Не удалось проверить модели')).toHaveCount(0);
  });
});

test.describe('мастер нового бота: черновик при уходе к провайдерам', () => {
  const DESC = 'Каждый день проверяй новые вакансии SRE и присылай краткий список';

  test('описание сохраняется в sessionStorage и возвращается после возврата', async ({ page }) => {
    await page.goto('/?mock=1&providers=none#/bots/new');
    await expect(page.getByRole('status').filter({ hasText: 'Сначала нужна модель' })).toBeVisible();
    await page.getByLabel('Что бот должен делать и как работать').fill(DESC);
    await page.getByRole('link', { name: 'Подключить модель' }).click();
    await expect(page).toHaveURL(/#\/settings\/providers$/);
    expect(await page.evaluate(() => sessionStorage.getItem('bothub_new_bot_draft'))).toBe(DESC);
    await page.goBack();
    await expect(page.getByLabel('Что бот должен делать и как работать')).toHaveValue(DESC);
  });

  test('есть модели: проверка тихая, «Собрать» работает, после создания черновик стирается', async ({ page }) => {
    await page.goto(`/?mock=1#/bots/new?d=${encodeURIComponent(DESC)}`);
    await expect(page.getByRole('status').filter({ hasText: 'Сначала нужна модель' })).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Собрать' })).toBeEnabled();
    await page.getByRole('button', { name: 'Собрать' }).click();
    await expect(page.getByLabel('Имя')).toBeVisible({ timeout: 5000 });
    await page.getByRole('button', { name: 'Создать', exact: true }).click();
    await expect(page).toHaveURL(/#\/threads\/t-draft-/);
    expect(await page.evaluate(() => sessionStorage.getItem('bothub_new_bot_draft'))).toBeNull();
  });
});

test.describe('состояние бота и провайдера согласовано', () => {
  test('ошибка провайдера в настройках бота: подсказка со ссылкой, новая модель её снимает', async ({ page }, testInfo) => {
    await page.goto('/?mock=1#/bots/archive'); // Архив: Ollama дома не отвечает
    const problem = page.locator('[data-model-problem-banner]');
    await expect(problem).toContainText('Ollama дома: не отвечает');
    await expect(problem.getByRole('link', { name: 'Открыть провайдера' })).toHaveAttribute('href', '#/settings/providers/p-ollama');
    await openPicker(page, testInfo);
    await page.locator('[role="radio"][data-provider="p-claude"]').filter({ hasText: 'Haiku 4.5' }).click();
    await expect(page.locator('[data-model-problem-banner]')).toHaveCount(0);
  });

  test('точка статуса у бота с ошибкой провайдера не зелёная, шапка треда называет провайдера из реестра', async ({ page }, testInfo) => {
    await page.goto('/?mock=1');
    const card = page.locator('.bot-card:has([data-bot="archive"]), .desktop-bot-row[data-bot="archive"]').first();
    await expect(card).toContainText('Провайдер: не отвечает');
    if (isMobile(testInfo)) {
      await expect(card.locator('.status-dot')).toHaveClass(/danger/);
      await expect(card.locator('.status-dot')).not.toHaveClass(/success/);
    }
    // шапка: «провайдер · модель · где работает», название провайдера из реестра (Claude Code), не имя раннера
    await page.locator('[data-action="open-thread"][data-bot="mac"]').first().click();
    await expect(page.locator('.app-header, .desktop-thread-head').filter({ hasText: 'Claude Code · Sonnet 5' }).first()).toBeVisible();
  });
});

test.describe('лист выбора модели: «Готово» на виду', () => {
  test('телефон: «Готово» видна без прокрутки даже на низком экране, варианты не меньше 44 px', async ({ page }, testInfo) => {
    test.skip(!isMobile(testInfo), 'лист только на телефоне');
    await page.setViewportSize({ width: 393, height: 520 });
    await page.goto('/?mock=1#/bots/mac');
    await page.locator('[data-pick-open]').click();
    const dialog = page.getByRole('dialog', { name: 'Модель бота' });
    await expect(dialog).toBeVisible();
    const done = dialog.getByRole('button', { name: 'Готово' });
    const box = await done.boundingBox();
    expect(box.y + box.height).toBeLessThanOrEqual(520.5);
    expect(box.height).toBeGreaterThanOrEqual(44);
    const small = await dialog.locator('[role="radio"]').evaluateAll((els) => els.map((el) => Math.round(el.getBoundingClientRect().height)).filter((h) => h < 44));
    expect(small).toEqual([]);
  });

  test('идёт задача: подзаголовок и пояснение согласованы с отказом сервера', async ({ page }, testInfo) => {
    await page.goto('/?mock=1#/bots/sre');
    if (isMobile(testInfo)) {
      await expect(page.locator('[data-pick-note]')).toHaveCount(0); // пояснение живёт внутри листа
      await page.locator('[data-pick-open]').click();
    }
    await expect(page.locator('[data-pick-note]')).toContainText('Модель можно сменить, когда бот закончит работу');
    if (isMobile(testInfo)) await expect(page.getByRole('dialog', { name: 'Модель бота' })).toContainText('идёт задача');
    await page.locator('[role="radio"][data-provider="p-claude"]').filter({ hasText: 'Sonnet 5' }).click();
    await expect(page.getByRole('alert').filter({ hasText: 'Сейчас идёт задача' })).toContainText('Модель можно сменить, когда бот закончит работу');
  });
});
