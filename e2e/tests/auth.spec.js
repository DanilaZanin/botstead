import { expect, test } from '@playwright/test';

// Мок-режим: ?mock=1 (по умолчанию вошёл админ), &role=member, &auth=none (старт без сессии), &setup=1 (нет пользователей).
// Пароли-триггеры мока (вход и текущий пароль при смене): wrong-password даёт 401, too-many-attempts даёт 429, server-down даёт 500.

const ADMIN = 'admin@example.org';
const GOOD_PASSWORD = 'correct-horse-battery';

async function fillLogin(page, email, password) {
  await page.getByLabel('Email', { exact: true }).fill(email);
  await page.getByLabel('Пароль', { exact: true }).fill(password);
  await page.getByRole('button', { name: /^(Войти|Повторить)$/ }).click();
}

async function openLogin(page, hash = '') {
  await page.goto(`/?mock=1&auth=none${hash}`);
  await expect(page.getByRole('heading', { name: 'botstead' })).toBeVisible();
}

test.describe('вход', () => {
  test('успешный вход возвращает на исходный маршрут', async ({ page }) => {
    await openLogin(page, '#/memory');
    await fillLogin(page, ADMIN, GOOD_PASSWORD);
    await expect(page).toHaveURL(/#\/memory$/);
    await expect(page.getByText(/Резюме EN для откликов/)).toBeVisible();
  });

  test('неверный пароль показывает ошибку у поля и остаётся на входе', async ({ page }) => {
    await openLogin(page);
    await fillLogin(page, ADMIN, 'wrong-password');
    await expect(page.getByText('Email или пароль не подходят')).toBeVisible();
    await expect(page.getByText('Логин или пароль не подходят')).toHaveCount(0);
    // текст стоит у поля пароля и связан с ним
    await expect(page.locator('#login-password-note')).toHaveText('Email или пароль не подходят');
    await expect(page.getByLabel('Пароль', { exact: true })).toHaveAttribute('aria-invalid', 'true');
    await expect(page.getByLabel('Пароль', { exact: true })).toBeFocused();
    await expect(page.getByRole('button', { name: 'Войти', exact: true })).toBeEnabled();
    await expect(page.getByRole('heading', { name: 'botstead' })).toBeVisible();
  });

  test('429: слишком много попыток', async ({ page }) => {
    await openLogin(page);
    await fillLogin(page, ADMIN, 'too-many-attempts');
    await expect(page.locator('#login-alert')).toContainText('Слишком много попыток');
    await expect(page.getByRole('button', { name: 'Войти', exact: true })).toBeEnabled();
    // фокус на сообщении: tabindex=-1, role=alert
    const message = page.locator('#login-alert [role="alert"]');
    await expect(message).toBeFocused();
    await expect(message).toHaveAttribute('tabindex', '-1');
  });

  test('ошибка сервера: сообщение и кнопка «Повторить»', async ({ page }) => {
    await openLogin(page);
    await fillLogin(page, ADMIN, 'server-down');
    await expect(page.locator('#login-alert')).toContainText('Ошибка сервера');
    await expect(page.locator('#login-alert')).toContainText('Подробности в логе ядра');
    await expect(page.locator('#login-alert')).not.toContainText('Сервер не отвечает');
    await expect(page.locator('#login-alert')).not.toContainText('500');
    await expect(page.locator('#login-alert [role="alert"]')).toBeFocused();
    await expect(page.getByRole('button', { name: 'Повторить' })).toBeVisible();
    // повтор с верным паролем проходит
    await page.getByLabel('Пароль', { exact: true }).fill(GOOD_PASSWORD);
    await page.getByRole('button', { name: 'Повторить' }).click();
    await expect(page.locator('[data-action="open-thread"][data-bot="scout"]').first()).toBeVisible();
  });

  test('пустые поля не уходят на сервер', async ({ page }) => {
    await openLogin(page);
    await page.getByRole('button', { name: 'Войти', exact: true }).click();
    await expect(page.getByText('Введите email')).toBeVisible();
  });

  test('экран токена владельца скрыт и открывается по ссылке', async ({ page }) => {
    await openLogin(page);
    await expect(page.getByLabel('Токен владельца')).toHaveCount(0);
    await page.getByRole('button', { name: 'Войти по токену владельца' }).click();
    await expect(page.getByLabel('Токен владельца')).toBeVisible();
    await page.getByLabel('Токен владельца').fill('stale-token');
    await page.getByRole('button', { name: 'Продолжить' }).click();
    await expect(page.getByText('Токен не подходит')).toBeVisible();
    await page.getByRole('button', { name: 'Войти по паролю' }).click();
    await expect(page.getByLabel('Email', { exact: true })).toBeVisible();
  });
});

test.describe('выход и смена пользователя', () => {
  test('выход из настроек показывает вход и не оставляет данные', async ({ page }) => {
    await page.goto('/?mock=1#/settings');
    await expect(page.getByRole('heading', { name: 'Настройки' })).toBeVisible();
    await page.getByRole('button', { name: 'Выйти' }).click();
    await expect(page.getByRole('heading', { name: 'botstead' })).toBeVisible();
    await expect(page.getByText('Скаут')).toHaveCount(0);
    // перезагрузка не возвращает сессию
    await page.goto('/?mock=1');
    await expect(page.getByRole('heading', { name: 'botstead' })).toBeVisible();
  });

  test('при смене пользователя локальные данные прошлого очищаются', async ({ page }) => {
    await page.goto('/?mock=1#/settings/users');
    await page.evaluate(() => localStorage.setItem('bothub_invite_notes', JSON.stringify({ x: 'заметка админа' })));
    await page.goto('/?mock=1#/settings');
    await page.getByRole('button', { name: 'Выйти' }).click();
    await expect(page.getByRole('heading', { name: 'botstead' })).toBeVisible();
    expect(await page.evaluate(() => localStorage.getItem('bothub_invite_notes'))).toBeNull();
    await fillLogin(page, 'alice@example.org', GOOD_PASSWORD);
    await expect(page.locator('[data-action="open-thread"][data-bot="scout"]').first()).toBeVisible();
    expect(await page.evaluate(() => localStorage.getItem('bothub_uid'))).toBe('u-alice');
  });
});

test.describe('смена пользователя без выхода', () => {
  test('другой user id в localStorage очищает локальные данные при старте', async ({ page }) => {
    await page.goto('/?mock=1');
    await expect(page.locator('[data-action="open-thread"][data-bot="scout"]').first()).toBeVisible();
    await page.evaluate(() => {
      localStorage.setItem('bothub_uid', 'u-other');
      localStorage.setItem('bothub_invite_notes', JSON.stringify({ x: 'чужая заметка' }));
    });
    await page.reload();
    await expect(page.locator('[data-action="open-thread"][data-bot="scout"]').first()).toBeVisible();
    expect(await page.evaluate(() => localStorage.getItem('bothub_invite_notes'))).toBeNull();
    expect(await page.evaluate(() => localStorage.getItem('bothub_uid'))).toBe('u-admin');
  });
});

test.describe('принятие инвайта', () => {
  async function fillInvite(page, email, password, repeat = password) {
    await page.getByLabel('Email', { exact: true }).fill(email);
    await page.getByLabel('Пароль', { exact: true }).fill(password);
    await page.getByLabel('Пароль ещё раз', { exact: true }).fill(repeat);
    await page.getByRole('button', { name: 'Создать аккаунт' }).click();
  }

  test('успех: аккаунт создан, вход выполнен, экран первого входа', async ({ page }) => {
    await page.goto('/?mock=1&auth=none#/invite/valid-token');
    await expect(page.getByRole('heading', { name: 'Приглашение' })).toBeVisible();
    await fillInvite(page, 'new@example.org', 'long-enough-pass');
    await expect(page).toHaveURL(/#\/welcome$/);
    await expect(page.getByRole('heading', { name: 'Аккаунт создан' })).toBeVisible();
    await expect(page.getByText('new@example.org')).toBeVisible();
    // шаг «Модель» ведёт в «Провайдеры» (каждый подключает свои модели); в моке модели уже включены, «Создать бота» активен
    await expect(page.locator('a[href="#/settings/providers"]').first()).toBeVisible();
    await expect(page.getByText(/Подключено моделей: \d+/)).toBeVisible();
    await expect(page.getByText('Модели подключает администратор сервера')).toHaveCount(0);
    await expect(page.getByRole('link', { name: 'Создать бота' })).toBeEnabled();
    await page.getByRole('link', { name: 'Позже' }).click();
    await expect(page.locator('[data-action="open-thread"][data-bot="scout"]').first()).toBeVisible();
  });

  test('ошибки ввода: короткий пароль и несовпадение', async ({ page }) => {
    await page.goto('/?mock=1&auth=none#/invite/valid-token');
    await fillInvite(page, 'new@example.org', 'short');
    await expect(page.getByText('Пароль короче 10 символов')).toBeVisible();
    await fillInvite(page, 'new@example.org', 'long-enough-pass', 'long-enough-pasX');
    await expect(page.getByText('Пароли не совпадают')).toBeVisible();
    await page.getByLabel('Email', { exact: true }).fill('alice@example.org');
    await page.getByLabel('Пароль ещё раз', { exact: true }).fill('long-enough-pass');
    await page.getByRole('button', { name: 'Создать аккаунт' }).click();
    await expect(page.getByText('Этот email уже занят')).toBeVisible();
  });

  test('истёкший или использованный инвайт', async ({ page }) => {
    await page.goto('/?mock=1&auth=none#/invite/expired-token');
    await expect(page.getByRole('heading', { name: 'Инвайт не работает' })).toBeVisible();
    await expect(page.getByLabel('Email', { exact: true })).toHaveCount(0);
    await page.getByRole('button', { name: 'Уже есть аккаунт: войти' }).click();
    await expect(page.getByRole('heading', { name: 'botstead' })).toBeVisible();
  });

  test('инвайт истёк между проверкой и отправкой формы', async ({ page }) => {
    await page.goto('/?mock=1&auth=none#/invite/race-expired-token');
    await fillInvite(page, 'late@example.org', 'long-enough-pass');
    await expect(page.getByRole('heading', { name: 'Инвайт истёк' })).toBeVisible();
    await page.goto('/?mock=1&auth=none#/invite/race-used-token');
    await page.reload();
    await fillInvite(page, 'late@example.org', 'long-enough-pass');
    await expect(page.getByRole('heading', { name: 'Инвайт уже использован' })).toBeVisible();
  });
});

test.describe('первичная настройка', () => {
  test('создание первого администратора с одноразовым кодом', async ({ page }) => {
    await page.goto('/?mock=1&setup=1');
    await expect(page.getByRole('heading', { name: 'Первая настройка' })).toBeVisible();
    await page.getByLabel('Email', { exact: true }).fill('boss@example.org');
    await page.getByLabel('Пароль', { exact: true }).fill('long-enough-pass');
    await page.getByLabel('Пароль ещё раз', { exact: true }).fill('long-enough-pass');
    await page.getByLabel('Одноразовый код').fill('wrong-code');
    await page.getByRole('button', { name: 'Создать администратора' }).click();
    await expect(page.getByText('Код не подходит')).toBeVisible();
    await page.getByLabel('Одноразовый код').fill('setup-code');
    await page.getByRole('button', { name: 'Создать администратора' }).click();
    await expect(page).toHaveURL(/#\/welcome$/);
    await expect(page.getByRole('heading', { name: 'Аккаунт создан' })).toBeVisible();
    // администратор, как и участник, подключает свои модели в разделе «Провайдеры»
    await expect(page.locator('a[href="#/settings/providers"]').first()).toBeVisible();
    await expect(page.getByText('Подключение своих моделей появится в разделе «Провайдеры»')).toHaveCount(0);
  });

  test('подсказка у кода: команда в моноширинном блоке с копированием', async ({ page, context }) => {
    await context.grantPermissions(['clipboard-read', 'clipboard-write']);
    await page.goto('/?mock=1&setup=1');
    const command = 'docker logs bothub-core 2>&1 | grep setup_code';
    const block = page.locator('#setup-cmd');
    await expect(block).toContainText(command);
    expect(await block.locator('code').evaluate((el) => getComputedStyle(el).fontFamily)).toMatch(/mono/i);
    await expect(page.getByLabel('Одноразовый код')).toHaveAttribute('aria-describedby', /setup-cmd/);
    await page.getByRole('button', { name: 'Скопировать команду' }).click();
    await expect(page.locator('#setup-cmd-status')).toHaveText('Команда скопирована');
    expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(command);
  });
});

test.describe('настройки', () => {
  test('админ видит раздел «Пользователи»', async ({ page }) => {
    await page.goto('/?mock=1#/settings');
    await expect(page.getByRole('link', { name: /Пользователи/ })).toBeVisible();
    await expect(page.getByText('Пользователями и инвайтами управляет администратор сервера.')).toHaveCount(0);
  });

  test('участник не видит раздел «Пользователи»', async ({ page }) => {
    await page.goto('/?mock=1&role=member#/settings');
    await expect(page.getByRole('heading', { name: 'Настройки' })).toBeVisible();
    await expect(page.getByRole('link', { name: /Пользователи/ })).toHaveCount(0);
    await expect(page.getByText('Пользователями и инвайтами управляет администратор сервера.')).toBeVisible();
    await page.goto('/?mock=1&role=member#/settings/users');
    await expect(page.getByText('Раздел только для администратора')).toBeVisible();
  });

  test('Разрешения: плашка «Скоро» и экран «Раздел в разработке»; Провайдеры и Активность рабочие разделы', async ({ page }) => {
    await page.goto('/?mock=1#/settings');
    await expect(page.locator('.settings-row .soon-pill')).toHaveCount(1);
    const providers = page.getByRole('link', { name: /^Провайдеры/ });
    await expect(providers).not.toHaveClass(/is-soon/);
    await expect(providers.locator('.soon-pill')).toHaveCount(0);
    const activity = page.locator('a[href="#/settings/activity"]');  // на Mac ссылка «Активность» есть и в боковой панели
    await expect(activity).not.toHaveClass(/is-soon/);
    await expect(activity.locator('.soon-pill')).toHaveCount(0);
    for (const name of ['Разрешения']) {
      const row = page.getByRole('link', { name: new RegExp(`^${name}`) });
      await expect(row).toHaveClass(/is-soon/);
      await expect(row.locator('.soon-pill')).toHaveText('Скоро');
      await expect(row.locator('.row-sub')).toHaveCount(0); // без подзаголовка-обещания
    }
    // строки с готовыми разделами остаются рабочими и без плашки
    await expect(page.getByRole('link', { name: /Память/ }).locator('.soon-pill')).toHaveCount(0);
    await providers.click();
    await expect(page).toHaveURL(/#\/settings\/providers$/);
    await expect(page.getByRole('heading', { name: 'Раздел в разработке' })).toHaveCount(0);
    await page.getByRole('link', { name: 'Назад' }).click();
    for (const [name, route] of [['Разрешения', 'permissions']]) {
      await page.getByRole('link', { name: new RegExp(`^${name}`) }).click();
      await expect(page).toHaveURL(new RegExp(`#/settings/${route}$`));
      await expect(page.getByRole('heading', { name: 'Раздел в разработке' })).toBeVisible();
      await expect(page.getByRole('heading', { name: 'Что можно сейчас' })).toBeVisible();
      await expect(page.getByText('Скоро', { exact: true })).toHaveCount(0);
      await page.getByRole('link', { name: 'К настройкам' }).click();
      await expect(page.getByRole('link', { name: /Память/ })).toBeVisible();
    }
  });

  test('смена пароля: неверный текущий, затем успех и выход', async ({ page }) => {
    await page.goto('/?mock=1#/settings/password');
    await page.getByLabel('Текущий пароль', { exact: true }).fill('wrong-password');
    await page.getByLabel('Новый пароль', { exact: true }).fill('new-password-456');
    await page.getByLabel('Новый пароль ещё раз', { exact: true }).fill('new-password-456');
    await page.getByRole('button', { name: 'Сменить пароль' }).click();
    await expect(page.getByText('Текущий пароль не подходит')).toBeVisible();
    await page.getByLabel('Текущий пароль', { exact: true }).fill('old-password-123');
    await page.getByRole('button', { name: 'Сменить пароль' }).click();
    await expect(page.getByRole('heading', { name: 'Пароль изменён' })).toBeVisible();
    await page.getByRole('button', { name: 'Войти' }).click();
    await expect(page.getByText('Пароль изменён. Войдите с новым паролем.')).toBeVisible();
    await expect(page.getByLabel('Email', { exact: true })).toHaveValue(ADMIN);
    await fillLogin(page, ADMIN, 'new-password-456');
    await expect(page.locator('[data-action="open-thread"][data-bot="scout"]').first()).toBeVisible();
  });

  test('мои сессии: завершение чужой сессии с подтверждением', async ({ page }) => {
    await page.goto('/?mock=1#/settings/sessions');
    await expect(page.getByText('iPhone · Safari')).toBeVisible();
    await expect(page.getByText('Mac · Chrome')).toBeVisible();
    await expect(page.getByText('Windows · Edge')).toHaveCount(0); // уже отозвана
    await page.getByRole('button', { name: 'Завершить сессию: Mac · Chrome' }).click();
    // одно касание ничего не закрывает: сначала подтверждение
    const dialog = page.getByRole('dialog', { name: 'Завершить сессию?' });
    await expect(dialog).toBeVisible();
    await expect(dialog).toContainText('потеряет доступ');
    await dialog.getByRole('button', { name: 'Отмена' }).click();
    await expect(page.getByRole('dialog')).toHaveCount(0);
    await expect(page.getByText('Mac · Chrome')).toBeVisible();
    await page.getByRole('button', { name: 'Завершить сессию: Mac · Chrome' }).click();
    await page.getByRole('dialog').getByRole('button', { name: 'Завершить', exact: true }).click();
    await expect(page.getByText('Mac · Chrome')).toHaveCount(0);
    await expect(page.getByText('iPhone · Safari')).toBeVisible();
  });

  test('завершение текущей сессии возвращает на вход', async ({ page }) => {
    await page.goto('/?mock=1#/settings/sessions');
    await page.getByRole('button', { name: 'Завершить сессию: iPhone · Safari' }).click();
    await page.getByRole('dialog').getByRole('button', { name: 'Завершить', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'botstead' })).toBeVisible();
  });
});

test.describe('пользователи и инвайты', () => {
  test('админ создаёт инвайт, видит ссылку один раз, копирует и отзывает', async ({ page, context }) => {
    await context.grantPermissions(['clipboard-read', 'clipboard-write']);
    await page.goto('/?mock=1#/settings/users');
    await expect(page.getByRole('heading', { name: /Пользователи · 3/ })).toBeVisible();
    await expect(page.getByRole('heading', { name: /Инвайты · 2/ })).toBeVisible();

    await page.getByRole('button', { name: 'Создать инвайт' }).click();
    const dialog = page.getByRole('dialog', { name: 'Новый инвайт' });
    await expect(dialog).toBeVisible();
    await dialog.getByLabel('Для кого').fill('Миша');
    await dialog.getByRole('radio', { name: '7 дней' }).click();
    await dialog.getByRole('button', { name: 'Создать ссылку' }).click();

    const linkDialog = page.getByRole('dialog', { name: 'Ссылка готова' });
    await expect(linkDialog).toBeVisible();
    await expect(linkDialog.getByText('Ссылка показывается один раз')).toBeVisible();
    const link = (await linkDialog.locator('#invite-link').textContent()).trim();
    expect(link).toMatch(/#\/invite\/mock-[a-z0-9]+$/);
    await linkDialog.getByRole('button', { name: 'Скопировать' }).click();
    await expect(linkDialog.getByRole('button', { name: 'Скопировано' })).toBeVisible();
    expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(link);

    await linkDialog.getByRole('button', { name: 'Закрыть' }).first().click();
    await expect(page.getByRole('dialog')).toHaveCount(0);
    await expect(page.getByRole('heading', { name: /Инвайты · 3/ })).toBeVisible();
    await expect(page.getByText('Инвайт: Миша')).toBeVisible();
    // токен нигде не остаётся на странице
    await expect(page.getByText(link)).toHaveCount(0);

    await page.getByRole('button', { name: 'Отозвать инвайт: Миша' }).click();
    const confirm = page.getByRole('dialog', { name: 'Отозвать инвайт?' });
    await expect(confirm).toContainText('Ссылка перестанет работать');
    await confirm.getByRole('button', { name: 'Отозвать', exact: true }).click();
    await expect(page.getByText('Инвайт: Миша')).toHaveCount(0);
    await expect(page.getByRole('heading', { name: /Инвайты · 2/ })).toBeVisible();
  });

  test('ссылка из инвайта открывает форму принятия', async ({ page }) => {
    await page.goto('/?mock=1#/settings/users');
    await page.getByRole('button', { name: 'Создать инвайт' }).click();
    await page.getByRole('dialog').getByRole('button', { name: 'Создать ссылку' }).click();
    const link = (await page.locator('#invite-link').textContent()).trim();
    const hash = link.slice(link.indexOf('#'));
    await page.evaluate((h) => { location.hash = h; }, hash);
    await expect(page.getByRole('heading', { name: 'Приглашение' })).toBeVisible();
    await expect(page.getByLabel('Email', { exact: true })).toBeVisible();
  });

  test('смена роли и отключение пользователя', async ({ page }) => {
    await page.goto('/?mock=1#/settings/users');
    await page.getByRole('button', { name: 'Действия: alice@example.org' }).click();
    const dialog = page.getByRole('dialog', { name: 'alice@example.org' });
    await dialog.getByRole('button', { name: 'Сделать админом' }).click();
    await expect(page.getByRole('dialog')).toHaveCount(0);
    await expect(page.getByRole('listitem').filter({ hasText: 'alice@example.org' })).toContainText('админ');
    await page.getByRole('button', { name: 'Действия: alice@example.org' }).click();
    await page.getByRole('dialog').getByRole('button', { name: 'Отключить' }).click();
    // отключение не срабатывает с одного касания
    const confirm = page.getByRole('dialog', { name: 'Отключить пользователя?' });
    await expect(confirm).toContainText('не сможет войти');
    await confirm.getByRole('button', { name: 'Отмена' }).click();
    await expect(page.getByRole('dialog', { name: 'alice@example.org' })).toBeVisible();
    await page.getByRole('dialog').getByRole('button', { name: 'Отключить' }).click();
    await page.getByRole('dialog').getByRole('button', { name: 'Отключить', exact: true }).click();
    await expect(page.getByRole('dialog')).toHaveCount(0);
    await expect(page.getByRole('listitem').filter({ hasText: 'alice@example.org' })).toContainText('Отключён');
    await page.getByRole('button', { name: 'Действия: alice@example.org' }).click();
    await page.getByRole('dialog').getByRole('button', { name: 'Включить' }).click();
    await expect(page.getByRole('listitem').filter({ hasText: 'alice@example.org' })).toContainText('Активен');
  });

  test('последнего администратора отключить нельзя: 409 с понятным текстом', async ({ page }) => {
    await page.goto('/?mock=1#/settings/users');
    await page.getByRole('button', { name: `Действия: ${ADMIN}` }).click();
    const dialog = page.getByRole('dialog');
    await dialog.getByRole('button', { name: 'Отключить' }).click();
    await dialog.getByRole('button', { name: 'Отключить', exact: true }).click();
    await expect(dialog.getByRole('alert')).toContainText('Нельзя изменить последнего администратора');
    await expect(dialog.getByRole('alert')).toContainText('Сначала назначьте другого администратора');
    await dialog.getByRole('button', { name: 'Отмена' }).click();
    await dialog.getByRole('button', { name: 'Сделать участником' }).click();
    await expect(dialog.getByRole('alert')).toContainText('Нельзя изменить последнего администратора');
    await page.keyboard.press('Escape');
    await expect(page.getByRole('dialog')).toHaveCount(0);
  });
});

test.describe('настройки бота на десктопе и телефоне', () => {
  test('форма настроек работает на любой ширине', async ({ page }) => {
    await page.goto('/?mock=1#/bots/scout');
    await expect(page.getByText('Настройки бота')).toBeVisible();
    await expect(page.getByText('Модель', { exact: true })).toBeVisible();
    await expect(page.getByText('Мозг')).toHaveCount(0);
    await expect(page.getByText(/открой на телефоне/)).toHaveCount(0);
    const mac = page.getByRole('radio', { name: 'Mac' });
    await mac.click();
    await expect(mac).toHaveAttribute('aria-checked', 'true');
  });
});

test.describe('service worker', () => {
  // Выполняем настоящий sw.js в песочнице страницы с подменой self и caches.
  async function runSw(page, scenario) {
    const source = await (await page.request.get('/sw.js')).text();
    const shell = source.match(/const CACHE = '([^']+)'/)[1];
    return page.evaluate(({ source: code, scenario: name, shell: shellName }) => {
      const handlers = {};
      const calls = { opened: [], deleted: [], respondWith: 0 };
      let store = [shellName, 'bothub-data-user-a', 'other-cache'];
      const fakeCaches = {
        open: async (key) => { calls.opened.push(key); return { put: async () => {}, addAll: async () => {} }; },
        match: async () => undefined,
        keys: async () => store.slice(),
        delete: async (key) => { calls.deleted.push(key); store = store.filter((k) => k !== key); return true; },
      };
      const fakeSelf = { addEventListener: (type, fn) => { handlers[type] = fn; }, skipWaiting() {}, clients: { claim() {} }, registration: {} };
      new Function('self', 'caches', 'fetch', code)(fakeSelf, fakeCaches, async () => new Response('x', { status: 200 }));
      const fetchEvent = (url, headers = {}) => ({ request: { method: 'GET', url, mode: 'cors', headers: new Headers(headers) }, respondWith: () => { calls.respondWith += 1; } });
      if (name === 'api') {
        handlers.fetch(fetchEvent('http://x/api/auth/me'));
        handlers.fetch(fetchEvent('http://x/api/bots'));
        handlers.fetch(fetchEvent('http://x/api/sessions'));
        handlers.fetch(fetchEvent('http://x/app.js', { authorization: 'Bearer t' }));
        return { respondWith: calls.respondWith, opened: calls.opened };
      }
      if (name === 'static') {
        handlers.fetch(fetchEvent('http://x/app.js'));
        return { respondWith: calls.respondWith };
      }
      if (name === 'clear') {
        let waited;
        handlers.message({ data: { type: 'clear-data' }, waitUntil: (p) => { waited = p; } });
        return waited.then(() => ({ deleted: calls.deleted, left: store, shell: shellName }));
      }
      return null;
    }, { source, scenario, shell });
  }

  test('ответы /api/auth/* и остальные /api/* не кэшируются и мимо service worker', async ({ page }) => {
    await page.goto('/?mock=1');
    const result = await runSw(page, 'api');
    expect(result.respondWith).toBe(0);
    expect(result.opened).toEqual([]);
  });

  test('статика проходит через service worker', async ({ page }) => {
    await page.goto('/?mock=1');
    expect((await runSw(page, 'static')).respondWith).toBe(1);
  });

  test('сообщение clear-data удаляет всё кроме оболочки', async ({ page }) => {
    await page.goto('/?mock=1');
    const result = await runSw(page, 'clear');
    expect(result.deleted.sort()).toEqual(['bothub-data-user-a', 'other-cache']);
    expect(result.left).toEqual([result.shell]);
  });
});
