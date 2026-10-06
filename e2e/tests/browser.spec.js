import { expect, test } from '@playwright/test';

// Экран «Браузер бота» (#/bots/<id>/browser): живой экран, три состояния управления, перехват и возврат, скрытый ввод
// пароля, лента шагов, состояния экрана, входы и перенаправление. Мок-режим: ?mock=1, бот «sre» (Claude, контейнер).
// Параметры: &browser=busy|down|lost|denied|none|conflict|bad|hold|ask|empty. Поддельный экран рисуется на canvas,
// счётчики ввода лежат в window.__screenMock, скрытый ввод и шаги управляются через window.__browserMock.

const isMobile = (testInfo) => testInfo.project.name === 'mobile-chromium';
const YOU = /(^|[^а-яё])(ты|тебя|тебе|тобой|твой|твоя|твоё|твои|твоих)(?![а-яё])|(Опиши|Выбери|Попробуй|Открой|Нажми|Введи|Смотри)(?![а-яё])/i;
const SECRET = 'Sup3r-Secret-Pass!';

// Пока экран грузится, #app закрыт для кликов (inert): жмём только после его снятия.
const ready = (page) => page.waitForFunction(() => { const app = document.getElementById('app'); return !!app && !app.inert; });

async function openBrowser(page, query = '', hash = '#/bots/sre/browser') {
  await page.goto(`/?mock=1${query}${hash}`);
  await ready(page);
}
async function waitLive(page) {
  await expect(page.locator('#br')).toHaveAttribute('data-screen', 'live', { timeout: 10_000 });
  await ready(page);
}
const screenMock = (page) => page.evaluate(() => ({ ...window.__screenMock, socket: undefined, close: undefined, freeBusy: undefined }));
const takeoverButton = (page) => page.getByRole('button', { name: 'Перехватить', exact: true });
const title = (page) => page.locator('#br-control-title');

async function takeOver(page) {
  await ready(page);
  await takeoverButton(page).click();
  const dialog = page.getByRole('dialog', { name: 'Перехватить управление?' });
  await expect(dialog).toBeVisible();
  await dialog.getByRole('button', { name: 'Перехватить', exact: true }).click();
  await expect(title(page)).toHaveText('Управляете вы');
  await ready(page);
}

test.describe('1. три состояния управления', () => {
  test('«Управляет бот», «Управляете вы», «Возврат боту»: вид, тексты, ввод', async ({ page }) => {
    await openBrowser(page, '&browser=hold');
    await waitLive(page);
    // Управляет бот: только просмотр, одна кнопка «Перехватить»
    await expect(title(page)).toHaveText('Управляет бот');
    await expect(page.locator('#br-control-sub')).toContainText('Экран только для просмотра');
    await expect(page.locator('#br-chip')).toHaveText('Только просмотр');
    await expect(takeoverButton(page)).toBeVisible();
    await expect(page.getByRole('button', { name: 'Вернуть боту' })).toHaveCount(0);
    expect((await screenMock(page)).viewOnly).toBe(true);

    // Управляете вы: ввод включён, шаг на паузе, кнопки «Вернуть боту» и «Пароль скрыто»
    await takeOver(page);
    await expect(page.locator('#br-control-sub')).toContainText('шаг на паузе');
    await expect(page.locator('#br-chip')).toHaveText('Ввод включён');
    await expect(page.getByRole('button', { name: 'Вернуть боту' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Пароль скрыто', exact: true })).toBeVisible();
    await expect(takeoverButton(page)).toHaveCount(0);
    expect((await screenMock(page)).viewOnly).toBe(false);

    // Возврат боту: ввод выключен, бот заново читает страницу
    await page.getByRole('button', { name: 'Вернуть боту' }).click();
    await expect(title(page)).toHaveText('Возврат боту');
    await expect(page.locator('#br-control-sub')).toContainText('бот заново читает страницу');
    await expect(page.locator('#br-chip')).toHaveText('Ввод выключен');
    await expect(page.getByRole('button', { name: 'Вернуть боту' })).toHaveCount(0);
    await expect(takeoverButton(page)).toBeVisible(); // из «возврата» управление можно перехватить снова
    expect((await screenMock(page)).viewOnly).toBe(true);

    // первый успешный снимок бота возвращает «Управляет бот»; сервер главнее: состояние подтягивается опросом
    await page.evaluate(() => window.__browserMock.snapshot());
    await expect(title(page)).toHaveText('Управляет бот');
    expect((await screenMock(page)).viewOnly).toBe(true);
  });

  test('расхождение с сервером: побеждает сервер', async ({ page }) => {
    await openBrowser(page);
    await waitLive(page);
    await expect(title(page)).toHaveText('Управляет бот');
    await page.evaluate(() => window.__browserMock.serverSet('human'));
    await expect(title(page)).toHaveText('Управляете вы');
    expect((await screenMock(page)).viewOnly).toBe(false);
    await page.evaluate(() => window.__browserMock.serverSet('bot'));
    await expect(title(page)).toHaveText('Управляет бот');
    expect((await screenMock(page)).viewOnly).toBe(true);
  });

  test('статус управления читается скринридером, экран подписан', async ({ page }) => {
    await openBrowser(page);
    await waitLive(page);
    const stage = page.locator('#br-stage');
    await expect(stage).toHaveAttribute('role', 'application');
    await expect(stage).toHaveAttribute('aria-label', /Экран браузера бота SRE/);
    const status = page.locator('#br-control');
    await expect(status).toHaveAttribute('role', 'status');
    await expect(status).toHaveAttribute('aria-live', 'polite');
  });
});

test.describe('2. перехват и возврат', () => {
  test('перехват с подтверждением: «Отмена» ничего не меняет, подтверждение передаёт управление', async ({ page }) => {
    await openBrowser(page);
    await waitLive(page);
    await takeoverButton(page).click();
    const dialog = page.getByRole('dialog', { name: 'Перехватить управление?' });
    await expect(dialog).toContainText('Текущая задача бота будет остановлена');
    await dialog.getByRole('button', { name: 'Отмена' }).click();
    await expect(dialog).toHaveCount(0);
    await expect(title(page)).toHaveText('Управляет бот');

    await takeoverButton(page).click();
    await expect(dialog).toBeVisible();
    const confirm = dialog.getByRole('button', { name: 'Перехватить', exact: true });
    await confirm.click();
    // состояние загрузки: кнопка занята, пока сервер отвечает
    await expect(dialog.locator('[data-confirm]')).toHaveAttribute('aria-busy', 'true');
    await expect(title(page)).toHaveText('Управляете вы');
    await expect(dialog).toHaveCount(0);
    // экран не пропал и не переподключался
    await expect(page.locator('#br')).toHaveAttribute('data-screen', 'live');
    expect((await screenMock(page)).connects).toBe(1);
  });

  test('возврат боту: кнопка занята, потом «Возврат боту»', async ({ page }) => {
    await openBrowser(page, '&browser=hold');
    await waitLive(page);
    await takeOver(page);
    const back = page.getByRole('button', { name: 'Вернуть боту' });
    await back.click();
    await expect(page.getByRole('button', { name: 'Возвращаю' })).toHaveAttribute('aria-busy', 'true');
    await expect(title(page)).toHaveText('Возврат боту');
  });

  test('отказ 502: «Управление не передано, бот продолжает работать»', async ({ page }) => {
    await openBrowser(page, '&browser=bad');
    await waitLive(page);
    await takeoverButton(page).click();
    const dialog = page.getByRole('dialog', { name: 'Перехватить управление?' });
    await dialog.getByRole('button', { name: 'Перехватить', exact: true }).click();
    await expect(dialog.getByRole('alert')).toContainText('Управление не передано, бот продолжает работать');
    await expect(title(page)).toHaveText('Управляет бот');
    expect((await screenMock(page)).viewOnly).toBe(true);
    // диалог остаётся открытым, закрыть можно «Отмена»
    await dialog.getByRole('button', { name: 'Отмена' }).click();
    await expect(dialog).toHaveCount(0);
  });

  test('отказ 409 invalid_transition: на экране состояние сервера', async ({ page }) => {
    await openBrowser(page, '&browser=conflict');
    await waitLive(page);
    await takeoverButton(page).click();
    const dialog = page.getByRole('dialog', { name: 'Перехватить управление?' });
    await dialog.getByRole('button', { name: 'Перехватить', exact: true }).click();
    await expect(dialog).toHaveCount(0);
    await expect(title(page)).toHaveText('Управляете вы');
    await expect(page.locator('#br-flash')).toContainText('Состояние уже изменилось');
  });

  test('сокет 4410: экран остаётся, подключение повторяется один раз', async ({ page }) => {
    await openBrowser(page);
    await waitLive(page);
    await page.evaluate(() => window.__screenMock.close(4410));
    await expect(page.locator('#br')).toHaveAttribute('data-screen', 'reconnecting');
    await expect(page.locator('#br-stage-wrap')).toBeVisible();
    await expect(page.locator('#br-last')).toBeVisible(); // последний кадр на месте
    await expect(page.locator('#br-overlay')).toContainText('Переподключаю экран');
    await waitLive(page);
    const state = await screenMock(page);
    expect(state.connects).toBe(2);
    expect(state.closes).toContain(4410);
    // второй 4410 подряд не переподключается бесконечно
    await page.evaluate(() => window.__screenMock.close(4410));
    await expect(page.getByRole('heading', { name: 'Связь с экраном потеряна' })).toBeVisible();
    expect((await screenMock(page)).connects).toBe(2);
  });
});

test.describe('3. скрытый ввод пароля', () => {
  async function openSecret(page) {
    await openBrowser(page);
    await waitLive(page);
    await takeOver(page);
    await page.getByRole('button', { name: 'Пароль скрыто', exact: true }).click();
    const dialog = page.getByRole('dialog', { name: 'Пароль для сайта' });
    await expect(dialog).toBeVisible();
    return dialog;
  }

  test('лист: гарантия, поле password без автозаполнения, пояснение «Что это значит»', async ({ page }) => {
    const dialog = await openSecret(page);
    await expect(dialog).toContainText('Пароль вводится напрямую в браузер бота. Модель не получает экран и не действует, пока управляете вы.');
    expect(await dialog.innerText()).not.toMatch(/не видит/i);
    const input = dialog.getByLabel('Пароль', { exact: true });
    await expect(input).toHaveAttribute('type', 'password');
    await expect(input).toHaveAttribute('autocomplete', 'off');
    await expect(input).toBeFocused();

    const meaning = dialog.getByText('Что это значит');
    await meaning.click();
    await expect(dialog).toContainText('Защита от модели');
    await expect(dialog).toContainText('может подсмотреть ввод');
    await expect(dialog).toContainText('отдельный пароль');

    // переключатель хранилища показывает имя секрета, имя проверяется
    const save = dialog.getByRole('switch', { name: /Сохранить в хранилище секретов/ });
    await expect(dialog.getByLabel('Имя секрета')).toBeHidden();
    await save.click();
    await expect(save).toBeChecked();
    const name = dialog.getByLabel('Имя секрета');
    await expect(name).toBeVisible();
    await input.fill(SECRET);
    await name.fill('1плохое имя');
    await dialog.getByRole('button', { name: 'Ввести', exact: true }).click();
    await expect(dialog.locator('#sec-name-note')).toHaveClass(/is-error/);
    await expect(input).toHaveValue(SECRET); // при ошибке проверки ничего не отправлено
    expect((await page.evaluate(() => window.__browserMock.secrets)).length).toBe(0);
  });

  test('отправка: значение уходит один раз и нигде не остаётся', async ({ page }) => {
    const dialog = await openSecret(page);
    const input = dialog.getByLabel('Пароль', { exact: true });
    await input.fill(SECRET);
    await dialog.getByRole('switch', { name: /Сохранить в хранилище секретов/ }).click();
    await dialog.getByLabel('Имя секрета').fill('site_password');
    await dialog.getByRole('button', { name: 'Ввести', exact: true }).click();
    await expect(dialog).toHaveCount(0);
    await expect(page.locator('#br-flash')).toContainText('Пароль введён в страницу');
    expect(await page.evaluate(() => window.__browserMock.secrets)).toEqual([{ length: SECRET.length, save_as: 'site_password' }]);

    expect(await page.content()).not.toContain(SECRET);
    expect(await page.locator('#app').innerText()).not.toContain(SECRET);
    const values = await page.evaluate(() => Array.from(document.querySelectorAll('input, textarea')).map((el) => el.value).join('|'));
    expect(values).not.toContain(SECRET);
    await expect(page.locator('input[type="password"]')).toHaveCount(0);
    const stored = await page.evaluate(() => JSON.stringify({ local: { ...localStorage }, session: { ...sessionStorage } }));
    expect(stored).not.toContain(SECRET);
    // в ленте шагов пароля нет
    expect(await page.evaluate(() => JSON.stringify(window.__browserMock))).not.toContain(SECRET);
  });

  test('отказ сервера: поле стёрто, текст ошибки, пароль нигде не виден', async ({ page }) => {
    const dialog = await openSecret(page);
    await page.evaluate(() => window.__browserMock.serverSet('bot')); // управление ушло к боту, пока лист был открыт
    const input = dialog.getByLabel('Пароль', { exact: true });
    await input.fill(SECRET);
    await dialog.getByRole('button', { name: 'Ввести', exact: true }).click();
    await expect(dialog.getByRole('alert')).toContainText('Управление уже у бота');
    await expect(input).toHaveValue('');
    expect(await page.content()).not.toContain(SECRET);
    expect((await page.evaluate(() => window.__browserMock.secrets)).length).toBe(0);
    await dialog.getByRole('button', { name: 'Отмена' }).click();
    await expect(dialog).toHaveCount(0);
  });

  test('пустой пароль не отправляется', async ({ page }) => {
    const dialog = await openSecret(page);
    await dialog.getByRole('button', { name: 'Ввести', exact: true }).click();
    await expect(dialog.locator('#sec-value-note')).toHaveText('Введите пароль.');
    expect((await page.evaluate(() => window.__browserMock.secrets)).length).toBe(0);
  });
});

test.describe('4. подтверждение действия бота', () => {
  test('карточка поверх экрана: что, куда, данные, можно ли отменить; «Отклонить» и «Разрешить»', async ({ page }) => {
    await openBrowser(page, '&browser=ask');
    await waitLive(page);
    const card = page.getByRole('region', { name: /Отправить форму входа на status\.example\.org/ });
    await expect(card).toBeVisible();
    await expect(card).toContainText('ОТПРАВКА');
    await expect(card).toContainText('Куда');
    await expect(card).toContainText('status.example.org/login');
    await expect(card).toContainText('Данные');
    await expect(card).toContainText('Логин, пароль из хранилища');
    await expect(card).toContainText('Отменить');
    await expect(card).toContainText('Нельзя');
    await expect(card).toContainText('страница не тронута');
    await expect(card.getByRole('button', { name: 'Разрешить' })).toBeVisible();
    await card.getByRole('button', { name: 'Отклонить' }).click();
    await expect(page.getByRole('region', { name: /Отправить форму входа/ })).toHaveCount(0);
    await expect(page.locator('#br-flash')).toContainText('Действие отклонено');
  });

  test('«Разрешить» убирает карточку', async ({ page }) => {
    await openBrowser(page, '&browser=ask');
    await waitLive(page);
    const card = page.getByRole('region', { name: /Отправить форму входа/ });
    await card.getByRole('button', { name: 'Разрешить' }).click();
    await expect(card).toHaveCount(0);
    await expect(page.locator('#br-flash')).toContainText('Действие разрешено');
  });

  test('без ожидающих подтверждений карточки нет; перехват сбрасывает ожидавшее', async ({ page }) => {
    await openBrowser(page);
    await waitLive(page);
    await expect(page.getByRole('region', { name: /Отправить форму входа/ })).toHaveCount(0);
    await openBrowser(page, '&browser=ask');
    await waitLive(page);
    const card = page.getByRole('region', { name: /Отправить форму входа/ });
    await expect(card).toBeVisible();
    // карточка не закрывает кнопку «Перехватить»; после перехвата ядро сбрасывает ожидавшие подтверждения
    await takeOver(page);
    await expect(card).toHaveCount(0);
  });
});

test.describe('5. лента шагов', () => {
  const list = (page, testInfo) => page.locator(`[data-steps-list][data-limit="${isMobile(testInfo) ? 3 : 200}"]`);

  test('новые сверху, ввод замаскирован, результат виден', async ({ page }, testInfo) => {
    await openBrowser(page);
    await waitLive(page);
    const items = list(page, testInfo).locator('.br-step');
    await expect(items).toHaveCount(isMobile(testInfo) ? 3 : 5);
    await expect(items.nth(0)).toContainText('Нажал');
    await expect(items.nth(0)).toContainText('кнопка «Войти»');
    await expect(items.nth(0)).toContainText('Ошибка');
    await expect(items.nth(1)).toContainText('Заполнил поле');
    await expect(items.nth(1)).toContainText('ввод скрыт');
    await expect(items.nth(1)).not.toContainText('redacted');
    await expect(items.nth(2)).toContainText('Нажал');
    await expect(items.nth(2)).toContainText('Готово');
    if (!isMobile(testInfo)) {
      await expect(items.nth(4)).toContainText('Открыл страницу');
      await expect(items.nth(4)).toContainText('https://status.example.org/');
    }
  });

  test('адрес в ленте и в строке адреса режется по середине', async ({ page }, testInfo) => {
    await openBrowser(page);
    await waitLive(page);
    const url = `https://jobs.example.eu/apply/${'x'.repeat(90)}/812`;
    await page.evaluate((value) => window.__browserMock.pushStep({ action: 'navigate', url: value }), url);
    const first = list(page, testInfo).locator('.br-step').first();
    await expect(first).toContainText('Открыл страницу');
    const cut = first.locator('.br-step-url');
    await expect(cut).toContainText('…');
    expect((await cut.innerText()).length).toBeLessThanOrEqual(38);
    await expect(cut).toHaveAttribute('title', url);
    expect(await cut.innerText()).toMatch(/^https:\/\/jobs\.example\.eu/);
    expect(await cut.innerText()).toMatch(/812$/);
    const address = page.locator('#br-addr');
    await expect(address).toContainText('…');
    await expect(address).toHaveAttribute('title', url);
    await expect(page.getByRole('group', { name: 'Адрес страницы, только чтение' })).toContainText('только чтение');
  });

  test('не больше 200 шагов в DOM, новые продолжают приходить', async ({ page }, testInfo) => {
    await openBrowser(page);
    await waitLive(page);
    await page.evaluate(() => window.__browserMock.pushSteps(230, 100));
    await expect(list(page, testInfo).locator('.br-step').first()).toContainText('элемент 329');
    let full;
    if (isMobile(testInfo)) {
      await page.getByRole('button', { name: 'Все шаги' }).click();
      full = page.getByRole('dialog', { name: 'Шаги' }).locator('[data-steps-list]');
    } else {
      full = page.locator('[data-steps-list][data-limit="200"]');
    }
    await expect(full.locator('.br-step')).toHaveCount(200);
    await page.evaluate(() => window.__browserMock.pushSteps(3, 400));
    await expect(full.locator('.br-step').first()).toContainText('элемент 402');
    await expect(full.locator('.br-step')).toHaveCount(200);
  });

  test('пустое состояние', async ({ page }) => {
    await openBrowser(page, '&browser=empty');
    await waitLive(page);
    await expect(page.getByText('Шагов пока нет').first()).toBeVisible();
    await expect(page.locator('.br-step')).toHaveCount(0);
    await expect(page.locator('#br-addr')).toContainText('Адрес появится после первого перехода');
    await page.evaluate(() => window.__browserMock.pushStep({ action: 'snapshot' }));
    await expect(page.locator('.br-step').first()).toContainText('Прочитал страницу');
    await expect(page.getByText('Шагов пока нет').first()).toBeHidden();
  });

  test('расход задачи рядом с состоянием', async ({ page }) => {
    await openBrowser(page);
    await waitLive(page);
    await expect(page.locator('#br-spend')).toContainText(/Расход задачи: \d+k токенов · \d\d:\d\d/);
  });
});

test.describe('6. состояния экрана', () => {
  test('подключение: «Подключаюсь к экрану», затем живой экран', async ({ page }) => {
    await page.addInitScript(() => {
      window.__seen = [];
      new MutationObserver(() => {
        const el = document.getElementById('br');
        const value = el && el.getAttribute('data-screen');
        if (value && window.__seen[window.__seen.length - 1] !== value) window.__seen.push(value);
      }).observe(document, { attributes: true, subtree: true, attributeFilter: ['data-screen'], childList: true });
    });
    await openBrowser(page);
    await waitLive(page);
    const seen = await page.evaluate(() => window.__seen);
    expect(seen.slice(0, 1)).toEqual(['connecting']);
    expect(seen[seen.length - 1]).toBe('live');
  });

  test('не запущен: «Пересоздать»', async ({ page }) => {
    await openBrowser(page, '&browser=down');
    await expect(page.getByRole('heading', { name: 'Компьютер бота не запущен' })).toBeVisible({ timeout: 10_000 });
    await expect(page.locator('#br-stage-wrap')).toBeHidden();
    await expect(page.getByRole('button', { name: 'Подключиться снова' })).toBeVisible();
    await page.getByRole('button', { name: /Пересоздать/ }).click();
    await expect(page.getByRole('heading', { name: 'Компьютер бота не запущен' })).toBeVisible({ timeout: 10_000 });
  });

  test('открыт на другом (4409): «Открыть здесь»', async ({ page }) => {
    await openBrowser(page, '&browser=busy');
    await expect(page.getByRole('heading', { name: 'Экран открыт на другом устройстве' })).toBeVisible({ timeout: 10_000 });
    await expect(page.getByText('Закройте его там или откройте здесь')).toBeVisible();
    await page.getByRole('button', { name: 'Открыть здесь' }).click();
    // другой клиент не ушёл: состояние остаётся, пояснение меняется
    await expect(page.getByText('по-прежнему не открывается')).toBeVisible({ timeout: 10_000 });
    await page.evaluate(() => window.__screenMock.freeBusy());
    await page.getByRole('button', { name: 'Открыть здесь' }).click();
    await waitLive(page);
  });

  test('открыт в другой вкладке этого браузера: «Открыть здесь» отпускает её экран', async ({ page, context }) => {
    await openBrowser(page);
    await waitLive(page);
    const other = await context.newPage();
    await other.goto('/?mock=1#/bots/sre/browser');
    await ready(other);
    await expect(other.locator('#br')).toHaveAttribute('data-screen', 'live', { timeout: 10_000 });
    await other.evaluate(() => new BroadcastChannel('botstead-screen').postMessage({ type: 'claim', bot: 'sre', from: 'другая-вкладка' }));
    await expect(page.getByRole('heading', { name: 'Экран открыт на другом устройстве' })).toBeVisible();
    await expect(page.getByText('Экран открыт в другой вкладке этого браузера')).toBeVisible();
    await expect(page.locator('#br-stage-wrap')).toBeHidden();
  });

  test('отказ до рукопожатия (браузер видит только 1006): «Открыть здесь» и «Пересоздать»', async ({ page }) => {
    await openBrowser(page, '&browser=refused');
    await expect(page.getByRole('heading', { name: 'Экран не открылся' })).toBeVisible({ timeout: 10_000 });
    await expect(page.getByRole('button', { name: 'Открыть здесь' })).toBeVisible();
    await expect(page.getByRole('button', { name: /Пересоздать/ })).toBeVisible();
    await expect(page.locator('#br-stage-wrap')).toBeHidden();
    await page.getByRole('button', { name: 'Открыть здесь' }).click();
    await expect(page.getByText('по-прежнему не открывается')).toBeVisible({ timeout: 10_000 });
  });

  test('нет доступа', async ({ page }) => {
    await openBrowser(page, '&browser=denied');
    await expect(page.getByRole('heading', { name: 'Нет доступа к экрану' })).toBeVisible({ timeout: 10_000 });
    await expect(page.getByRole('link', { name: 'К списку ботов' })).toBeVisible();
    await expect(page.locator('#br-stage-wrap')).toBeHidden();
    await expect(page.locator('#br-control')).toBeHidden();
  });

  test('потеряна связь: «Подключиться снова»', async ({ page }) => {
    await openBrowser(page, '&browser=lost');
    await expect(page.getByRole('heading', { name: 'Связь с экраном потеряна' })).toBeVisible({ timeout: 10_000 });
    const before = (await screenMock(page)).connects;
    await page.getByRole('button', { name: 'Подключиться снова' }).click();
    await expect(page.getByRole('heading', { name: 'Связь с экраном потеряна' })).toBeVisible({ timeout: 10_000 });
    expect((await screenMock(page)).connects).toBe(before + 1);
  });

  test('нет браузера: у бота на другой модели или на Mac', async ({ page }) => {
    await openBrowser(page, '', '#/bots/scout/browser');
    await expect(page.getByRole('heading', { name: 'У этого бота нет браузера' })).toBeVisible();
    await expect(page.locator('#br-stage-wrap')).toBeHidden();
    await expect(page.getByRole('link', { name: 'Настройки бота' })).toBeVisible();
    await openBrowser(page, '&browser=none');
    await expect(page.getByRole('heading', { name: 'У этого бота нет браузера' })).toBeVisible();
    await openBrowser(page, '', '#/bots/mac/browser');
    await expect(page.getByRole('heading', { name: 'У этого бота нет браузера' })).toBeVisible();
  });

  test('неизвестный бот: нет доступа', async ({ page }) => {
    await openBrowser(page, '', '#/bots/ghost/browser');
    await expect(page.getByRole('heading', { name: 'Нет доступа к экрану' })).toBeVisible();
  });
});

test.describe('7. входы и перенаправление', () => {
  test('смена hash с экрана браузера открывает тред без перезагрузки', async ({ page }) => {
    await openBrowser(page, '&browser=refused');
    await expect(page.getByRole('heading', { name: 'Экран не открылся' })).toBeVisible({ timeout: 10_000 });
    await page.evaluate(() => { location.hash = '#/threads/t-sre'; });
    await ready(page);
    await expect(page.locator('#composer-input')).toBeVisible();
    await expect(page.locator('#composer-input')).toBeEnabled();
    await expect(page.getByRole('heading', { name: /Браузер · SRE/ })).toHaveCount(0);
  });

  test('из треда: «Экран» ведёт на экран браузера', async ({ page }, testInfo) => {
    await page.goto('/?mock=1#/threads/t-sre');
    await ready(page);
    await expect(page.locator('#composer-input')).toBeEnabled();
    if (isMobile(testInfo)) {
      await page.getByRole('link', { name: 'Экран бота', exact: true }).click();
    } else {
      // боковая панель: состояние экрана и кнопка
      await expect(page.locator('[data-browser-status]')).toHaveText('Управляет бот');
      await page.getByRole('link', { name: 'Открыть экран' }).click();
      await expect(page).toHaveURL(/#\/bots\/sre\/browser$/);
      await page.goBack();
      await ready(page);
      await page.getByRole('link', { name: 'Экран', exact: true }).click();
    }
    await expect(page).toHaveURL(/#\/bots\/sre\/browser$/);
    await ready(page);
    await expect(title(page)).toHaveText('Управляет бот');
  });

  test('у бота без браузера входа в тред-шапке нет', async ({ page }, testInfo) => {
    await page.goto('/?mock=1#/threads/t-scout');
    await ready(page);
    await expect(page.locator('#composer-input')).toBeEnabled();
    await expect(page.getByRole('link', { name: /^Экран/ })).toHaveCount(0);
    if (!isMobile(testInfo)) await expect(page.getByText('У этого бота нет браузера')).toBeVisible();
  });

  test('из карточки бота', async ({ page }, testInfo) => {
    test.skip(!isMobile(testInfo), 'карточки ботов на списке только в телефонной раскладке');
    await page.goto('/?mock=1');
    await ready(page);
    await expect(page.getByRole('link', { name: 'Экран бота: Скаут' })).toHaveCount(0);
    await page.getByRole('link', { name: 'Экран бота: SRE' }).click();
    await expect(page).toHaveURL(/#\/bots\/sre\/browser$/);
    await ready(page);
    await expect(page.getByRole('heading', { name: /Браузер · SRE/ })).toBeVisible();
  });

  test('старый адрес #/threads/<id>/handoff ведёт на экран браузера бота', async ({ page }) => {
    await page.goto('/?mock=1#/threads/t-sre/handoff');
    await ready(page);
    await expect(page).toHaveURL(/#\/bots\/sre\/browser$/);
    await expect(title(page)).toHaveText('Управляет бот');
    await page.goto('/?mock=1#/threads/t-mac/handoff');
    await ready(page);
    await expect(page).toHaveURL(/#\/bots\/mac\/browser$/);
    await expect(page.getByRole('heading', { name: 'У этого бота нет браузера' })).toBeVisible();
    await page.goto('/?mock=1#/threads/нет-такого/handoff');
    await ready(page);
    await expect(page).toHaveURL(/#\/$/);
  });

  test('«назад» из экрана ведёт в тред бота (телефон)', async ({ page }, testInfo) => {
    test.skip(!isMobile(testInfo), 'шапка с «Назад» только в телефонной раскладке');
    await openBrowser(page);
    await page.getByRole('link', { name: 'Назад' }).click();
    await expect(page).toHaveURL(/#\/threads\/t-sre$/);
  });
});

test.describe('8. раскладка и доступность', () => {
  test('телефон: экран сверху 16:10, главное действие внизу, кнопки не меньше 44 px', async ({ page }, testInfo) => {
    test.skip(!isMobile(testInfo), 'только телефонная раскладка');
    await openBrowser(page);
    await waitLive(page);
    const stage = await page.locator('#br-stage').boundingBox();
    expect(stage.width / stage.height).toBeGreaterThan(1.55);
    expect(stage.width / stage.height).toBeLessThan(1.65);
    expect(stage.y).toBeLessThan(140);
    const action = await takeoverButton(page).boundingBox();
    expect(action.height).toBeGreaterThanOrEqual(44);
    expect(action.y + action.height).toBeGreaterThan(852 - 120);
    for (const act of ['scale', 'fullscreen']) {
      const box = await page.locator(`[data-br="${act}"]`).boundingBox();
      expect(box.width).toBeGreaterThanOrEqual(44);
      expect(box.height).toBeGreaterThanOrEqual(44);
    }
    const all = await page.getByRole('button', { name: 'Все шаги' }).boundingBox();
    expect(all.height).toBeGreaterThanOrEqual(44);
    const scroll = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    expect(scroll).toBeLessThanOrEqual(0);
  });

  test('Mac: экран в центре, лента справа, кнопки не меньше 44 px', async ({ page }, testInfo) => {
    test.skip(isMobile(testInfo), 'только раскладка Mac');
    await openBrowser(page);
    await waitLive(page);
    const stage = await page.locator('#br-stage').boundingBox();
    const aside = await page.locator('.desktop-aside').boundingBox();
    const sidebar = await page.locator('.desktop-sidebar').boundingBox();
    expect(stage.x).toBeGreaterThanOrEqual(sidebar.x + sidebar.width - 1);
    expect(stage.x + stage.width).toBeLessThanOrEqual(aside.x + 1);
    await expect(page.locator('.desktop-aside [data-steps-list]')).toBeVisible();
    for (const locator of [takeoverButton(page), page.locator('[data-br="scale"]'), page.locator('[data-br="fullscreen"]')]) {
      const box = await locator.boundingBox();
      expect(box.height).toBeGreaterThanOrEqual(44);
    }
  });

  test('масштаб и полный экран переключаются', async ({ page }) => {
    await openBrowser(page);
    await waitLive(page);
    const scale = page.locator('[data-br="scale"]');
    await expect(scale).toHaveAttribute('aria-pressed', 'true');
    await expect(scale).toHaveAccessibleName('Масштаб: по размеру окна');
    await scale.click();
    await expect(scale).toHaveAttribute('aria-pressed', 'false');
    await expect(scale).toHaveAccessibleName('Масштаб: 100%');
    expect((await screenMock(page)).fit).toBe(false);
    await scale.click();
    expect((await screenMock(page)).fit).toBe(true);

    const full = page.locator('[data-br="fullscreen"]');
    await expect(full).toHaveAttribute('aria-pressed', 'false');
    await full.click();
    await expect(full).toHaveAttribute('aria-pressed', 'true');
    await expect(page.locator('#br')).toHaveClass(/is-fs/);
    await full.click();
    await expect(full).toHaveAttribute('aria-pressed', 'false');
    await expect(page.locator('#br')).not.toHaveClass(/is-fs/);
  });

  test('в интерфейсе экрана нет обращения на «ты»', async ({ page }) => {
    for (const query of ['', '&browser=down', '&browser=denied', '&browser=none', '&browser=ask']) {
      await openBrowser(page, query);
      await expect.poll(async () => (await page.locator('#app').innerText()).length, { message: query }).toBeGreaterThan(40);
      await page.waitForTimeout(500);
      const text = await page.locator('#app').innerText();
      const attrs = await page.locator('#app [aria-label], #app [title]').evaluateAll((els) => els.map((el) => `${el.getAttribute('aria-label') || ''} ${el.getAttribute('title') || ''}`));
      const found = [text, ...attrs].map((t) => t.match(YOU)?.[0]).filter(Boolean);
      expect(found, `${query}: ${found.join(', ')}`).toEqual([]);
    }
    await openBrowser(page);
    await waitLive(page);
    await takeOver(page);
    await page.getByRole('button', { name: 'Пароль скрыто', exact: true }).click();
    const text = await page.getByRole('dialog', { name: 'Пароль для сайта' }).innerText();
    expect(text.match(YOU)?.[0]).toBeUndefined();
    expect(text).not.toMatch(/не видит/i);
  });
});

test.describe('9. клавиатура на Mac', () => {
  test.beforeEach(async ({}, testInfo) => {
    test.skip(isMobile(testInfo), 'клавиатурный сценарий только для раскладки Mac');
  });

  test('в режиме просмотра экран не забирает клавиши: Tab проходит дальше', async ({ page }) => {
    await openBrowser(page);
    await waitLive(page);
    const stage = page.locator('#br-stage');
    await stage.focus();
    await expect(stage).toBeFocused();
    await expect(page.locator('#br-kbd-hint')).toContainText('Режим просмотра');
    await page.keyboard.press('Enter');
    await expect(stage).not.toHaveClass(/is-capturing/);
    await page.keyboard.press('a');
    expect((await screenMock(page)).keys).toBe(0);
    await page.keyboard.press('Tab');
    await expect(stage).not.toBeFocused();
  });

  test('при управлении человеком: Enter включает ввод в экран, Shift+Esc выходит, Esc остаётся в экране', async ({ page }) => {
    await openBrowser(page);
    await waitLive(page);
    await takeOver(page);
    const stage = page.locator('#br-stage');
    await stage.focus();
    await expect(page.locator('#br-kbd-hint')).toContainText('Shift+Esc');
    await page.keyboard.press('Enter');
    await expect(stage).toHaveClass(/is-capturing/);
    await expect(page.locator('#br-kbd-hint')).toContainText('Ввод идёт в браузер бота');
    await page.keyboard.press('a');
    await page.keyboard.press('Tab'); // Tab уходит в браузер бота, а не по странице
    await expect(stage).toHaveClass(/is-capturing/);
    await page.keyboard.press('Escape'); // обычный Esc тоже уходит в экран
    const log = (await screenMock(page)).keyLog;
    expect(log).toContain('char');
    expect(log).toContain('Tab');
    expect(log).toContain('Escape');

    await page.keyboard.press('Shift+Escape');
    await expect(stage).not.toHaveClass(/is-capturing/);
    await expect(stage).toBeFocused();
    const after = (await screenMock(page)).keyLog;
    expect(after.filter((key) => key === 'Escape').length).toBe(1); // Shift+Esc экрану не передан
    await page.keyboard.press('Tab');
    await expect(stage).not.toBeFocused();
  });
});

// ---------------------------------------------------------------------------
// 10. Правки по UX-оценке экрана
// ---------------------------------------------------------------------------
// Цвет токена так, как его считает браузер (rgb(...)): сравнение не зависит от светлой и тёмной темы.
const tokenColor = (page, value) => page.evaluate((v) => {
  const probe = document.createElement('i');
  probe.style.color = v;
  document.body.append(probe);
  const out = getComputedStyle(probe).color;
  probe.remove();
  return out;
}, value);
const backBtn = (page) => page.getByRole('button', { name: 'Вернуть боту' });

test.describe('10.1 обрыв связи при ручном управлении', () => {
  test('lost: карточка, «Вернуть боту» без кнопки пароля, текст про паузу бота', async ({ page }) => {
    await openBrowser(page);
    await waitLive(page);
    await takeOver(page);
    await page.evaluate(() => window.__screenMock.close(1006));
    await expect(page.getByRole('heading', { name: 'Связь с экраном потеряна' })).toBeVisible();
    await expect(page.locator('#br-state')).toContainText('Бот на паузе, пока управление у вас');
    await expect(page.locator('#br-state')).not.toContainText('Бот мог продолжить работу');
    await expect(backBtn(page)).toBeVisible();
    await expect(page.getByRole('button', { name: 'Пароль скрыто', exact: true })).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Подключиться снова' })).toBeVisible();
    // возврат работает и без картинки
    await backBtn(page).click();
    await expect(page.locator('#br')).toHaveAttribute('data-control', 'returning');
    await expect(backBtn(page)).toHaveCount(0);
  });

  test('busy (4409): карточка и «Вернуть боту»', async ({ page }) => {
    await openBrowser(page);
    await waitLive(page);
    await takeOver(page);
    await page.evaluate(() => { window.__screenMock.busy = true; window.__screenMock.close(4409); });
    await expect(page.getByRole('heading', { name: 'Экран открыт на другом устройстве' })).toBeVisible({ timeout: 10_000 });
    await expect(page.locator('#br-state')).toContainText('Бот на паузе, пока управление у вас');
    await expect(backBtn(page)).toBeVisible();
    await expect(page.getByRole('button', { name: 'Пароль скрыто', exact: true })).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Открыть здесь' })).toBeVisible();
  });

  for (const [query, heading] of [['&browser=down', 'Компьютер бота не запущен'], ['&browser=refused', 'Экран не открылся']]) {
    test(`${query}: управление у человека (подтягивается с сервера), «Вернуть боту» есть`, async ({ page }) => {
      await openBrowser(page, query);
      await expect(page.getByRole('heading', { name: heading })).toBeVisible({ timeout: 10_000 });
      await expect(backBtn(page)).toHaveCount(0); // у бота кнопки нет
      await page.evaluate(() => window.__browserMock.serverSet('human'));
      await expect(backBtn(page)).toBeVisible({ timeout: 10_000 });
      await expect(page.locator('#br-state')).toContainText('Бот на паузе, пока управление у вас');
      await expect(page.getByRole('button', { name: 'Пароль скрыто', exact: true })).toHaveCount(0);
      await page.evaluate(() => window.__browserMock.serverSet('bot'));
      await expect(backBtn(page)).toHaveCount(0, { timeout: 10_000 });
      await expect(page.locator('#br-state')).not.toContainText('Бот на паузе');
    });
  }

  test('lost при управлении бота: прежний текст, кнопки возврата нет', async ({ page }) => {
    await openBrowser(page, '&browser=lost');
    await expect(page.getByRole('heading', { name: 'Связь с экраном потеряна' })).toBeVisible({ timeout: 10_000 });
    await expect(page.locator('#br-state')).toContainText('Бот мог продолжить работу');
    await expect(page.locator('#br-state')).not.toContainText('Бот на паузе');
    await expect(backBtn(page)).toHaveCount(0);
  });
});

test.describe('10.2 двойной тап по «Перехватить»', () => {
  test('после перехвата «Вернуть боту» занята 600 мс, фокус на экране', async ({ page }) => {
    await openBrowser(page, '&browser=hold');
    await waitLive(page);
    await takeoverButton(page).click();
    const dialog = page.getByRole('dialog', { name: 'Перехватить управление?' });
    await dialog.getByRole('button', { name: 'Перехватить', exact: true }).click();
    await expect(title(page)).toHaveText('Управляете вы');
    await expect(dialog).toHaveCount(0);
    const back = backBtn(page);
    await expect(back).toBeDisabled();
    await expect(page.locator('#br-stage')).toBeFocused();
    await expect(back).not.toBeFocused();
    // второй тап, пришедший уже на кнопку «Вернуть боту», ничего не делает
    await back.click({ force: true });
    await page.waitForTimeout(200);
    await expect(title(page)).toHaveText('Управляете вы');
    // через 600 мс кнопка снова рабочая
    await expect(back).toBeEnabled();
    await back.click();
    await expect(title(page)).toHaveText('Возврат боту');
  });
});

test.describe('10.3 рамка захвата ввода', () => {
  const frame = (page) => page.locator('#br-stage').evaluate((el) => {
    const after = getComputedStyle(el, '::after');
    return { content: after.content, position: after.position, z: after.zIndex, events: after.pointerEvents, shadow: after.boxShadow, edges: [after.top, after.right, after.bottom, after.left], outline: getComputedStyle(el).outlineStyle };
  });

  test('рамка рисуется ::after поверх экрана: фокус синий, захват янтарный', async ({ page }) => {
    await openBrowser(page);
    await waitLive(page);
    await takeOver(page);
    const stage = page.locator('#br-stage');
    // фокус с клавиатуры: Tab вперёд и Shift+Tab назад
    await stage.focus();
    await page.keyboard.press('Tab');
    await page.keyboard.press('Shift+Tab');
    await expect(stage).toBeFocused();
    const focusRing = await frame(page);
    expect(focusRing.content).toBe('""');
    expect(focusRing.position).toBe('absolute');
    expect(focusRing.z).toBe('3');
    expect(focusRing.events).toBe('none');
    expect(focusRing.edges).toEqual(['0px', '0px', '0px', '0px']);
    expect(focusRing.outline).toBe('none'); // outline блока перекрывался холстом
    expect(focusRing.shadow).toContain('inset');
    expect(focusRing.shadow).toContain(await tokenColor(page, 'var(--focus)'));

    // захват ввода: клик по экрану
    await page.locator('.br-mock-canvas').click();
    await expect(stage).toHaveClass(/is-capturing/);
    const capture = await frame(page);
    expect(capture.shadow).toContain('inset');
    expect(capture.shadow).toContain(await tokenColor(page, 'var(--attention-fg)'));
    expect(capture.outline).toBe('none');
  });
});

test.describe('10.4 инструменты вне экрана', () => {
  test('кнопки в полосе над экраном в ряду с адресом, не на картинке, не меньше 44 px', async ({ page }) => {
    await openBrowser(page);
    await waitLive(page);
    await expect(page.locator('#br-stage-wrap .br-tools')).toHaveCount(0);
    await expect(page.locator('#br-strip .br-tools')).toHaveCount(1);
    const stage = await page.locator('#br-stage').boundingBox();
    const addr = await page.locator('#br-addr-group').boundingBox();
    for (const act of ['scale', 'fullscreen']) {
      const box = await page.locator(`[data-br="${act}"]`).boundingBox();
      expect(box.width).toBeGreaterThanOrEqual(44);
      expect(box.height).toBeGreaterThanOrEqual(44);
      expect(box.y + box.height).toBeLessThanOrEqual(stage.y + 1); // над экраном, а не на нём
      expect(Math.abs((box.y + box.height / 2) - (addr.y + addr.height / 2))).toBeLessThan(10); // в одном ряду с адресом
    }
    expect(addr.y + addr.height).toBeLessThanOrEqual(stage.y + 1);
  });

  test('в ручном режиме правый верхний угол экрана принимает клики', async ({ page }) => {
    await openBrowser(page);
    await waitLive(page);
    await takeOver(page);
    const before = (await screenMock(page)).clicks;
    const box = await page.locator('#br-stage').boundingBox();
    await page.locator('#br-stage').click({ position: { x: box.width - 8, y: 8 } });
    expect((await screenMock(page)).clicks).toBe(before + 1);
  });

  test('во весь экран инструменты поверх экрана, после выхода возвращаются в полосу', async ({ page }) => {
    await openBrowser(page);
    await waitLive(page);
    const full = page.locator('[data-br="fullscreen"]');
    await full.click();
    await expect(page.locator('#br')).toHaveClass(/is-fs/);
    await expect(page.locator('#br-stage-wrap .br-tools')).toHaveCount(1);
    await expect(page.locator('#br-strip .br-tools')).toHaveCount(0);
    const stage = await page.locator('#br-stage').boundingBox();
    const box = await full.boundingBox();
    expect(box.y).toBeGreaterThanOrEqual(stage.y);
    expect(box.y + box.height).toBeLessThanOrEqual(stage.y + stage.height);
    await full.click();
    await expect(page.locator('#br')).not.toHaveClass(/is-fs/);
    await expect(page.locator('#br-strip .br-tools')).toHaveCount(1);
    await expect(page.locator('#br-stage-wrap .br-tools')).toHaveCount(0);
  });

  test('вид icon-btn sunken', async ({ page }) => {
    await openBrowser(page);
    await waitLive(page);
    for (const act of ['scale', 'fullscreen']) {
      const button = page.locator(`[data-br="${act}"]`);
      await expect(button).toHaveClass(/icon-btn/);
      await expect(button).toHaveClass(/sunken/);
      expect(await button.evaluate((el) => getComputedStyle(el).backgroundColor)).toBe(await tokenColor(page, 'var(--bg-sunken)'));
    }
  });
});

test.describe('10.5 метка «кто управляет» на экране', () => {
  test('плашка в левом нижнем углу, для скринридера скрыта, при ручном управлении янтарная', async ({ page }) => {
    await openBrowser(page, '&browser=hold');
    await waitLive(page);
    const who = page.locator('#br-who');
    await expect(who).toBeVisible();
    await expect(who).toHaveAttribute('aria-hidden', 'true');
    await expect(who).toHaveText('Управляет бот');
    const wrap = await page.locator('#br-stage-wrap').boundingBox();
    const plate = await who.boundingBox();
    expect(plate.x - wrap.x).toBeGreaterThanOrEqual(0);
    expect(plate.x - wrap.x).toBeLessThan(20);
    expect(wrap.y + wrap.height - (plate.y + plate.height)).toBeGreaterThanOrEqual(0);
    expect(wrap.y + wrap.height - (plate.y + plate.height)).toBeLessThan(20);
    const botBg = await who.evaluate((el) => getComputedStyle(el).backgroundColor);

    await takeOver(page);
    await expect(who).toHaveText('Управляете вы');
    expect(await who.evaluate((el) => getComputedStyle(el).backgroundColor)).toBe(await tokenColor(page, 'var(--attention-bg)'));
    expect(await who.evaluate((el) => getComputedStyle(el).color)).toBe(await tokenColor(page, 'var(--attention-text)'));
    expect(botBg).not.toBe(await tokenColor(page, 'var(--attention-bg)'));

    await backBtn(page).click();
    await expect(who).toHaveText('Возврат управления');
    await page.evaluate(() => window.__browserMock.snapshot());
    await expect(who).toHaveText('Управляет бот');
  });

  test('во весь экран видны плашка и главное действие; «Перехватить» выходит из полного экрана и спрашивает', async ({ page }) => {
    await openBrowser(page, '&browser=hold');
    await waitLive(page);
    const fsAction = page.locator('#br-fs-action');
    await expect(fsAction).toBeHidden();
    await page.locator('[data-br="fullscreen"]').click();
    await expect(page.locator('#br')).toHaveClass(/is-fs/);
    await expect(page.locator('#br-who')).toBeVisible();
    const take = fsAction.getByRole('button', { name: 'Перехватить', exact: true });
    await expect(take).toBeVisible();
    await take.click();
    await expect(page.locator('#br')).not.toHaveClass(/is-fs/);
    const dialog = page.getByRole('dialog', { name: 'Перехватить управление?' });
    await expect(dialog).toBeVisible();
    await dialog.getByRole('button', { name: 'Перехватить', exact: true }).click();
    await expect(title(page)).toHaveText('Управляете вы');

    await page.locator('[data-br="fullscreen"]').click();
    await expect(page.locator('#br')).toHaveClass(/is-fs/);
    await expect(page.locator('#br-who')).toHaveText('Управляете вы');
    const back = fsAction.getByRole('button', { name: 'Вернуть боту' });
    await expect(back).toBeVisible();
    await back.click();
    await expect(title(page)).toHaveText('Возврат боту');
    await expect(fsAction.getByRole('button', { name: 'Перехватить', exact: true })).toBeVisible();
    await page.locator('[data-br="fullscreen"]').click();
    await expect(page.locator('#br')).not.toHaveClass(/is-fs/);
    await expect(fsAction).toBeHidden();
  });
});

test.describe('10.6 клавиатура с телефона', () => {
  const kbdButton = (page) => page.getByRole('button', { name: 'Клавиатура', exact: true });
  const keyboardLog = async (page) => (await screenMock(page)).keyLog || [];

  test('вне ручного режима кнопки нет', async ({ page }) => {
    await page.setViewportSize({ width: 600, height: 900 });
    await openBrowser(page, '&browser=hold');
    await waitLive(page);
    await expect(kbdButton(page)).toHaveCount(0);
    await takeOver(page);
    await expect(kbdButton(page)).toBeVisible();
    await backBtn(page).click();
    await expect(title(page)).toHaveText('Возврат боту');
    await expect(kbdButton(page)).toHaveCount(0);
  });

  test('на широком экране с мышью кнопка скрыта', async ({ page }, testInfo) => {
    test.skip(isMobile(testInfo), 'только раскладка Mac');
    await openBrowser(page);
    await waitLive(page);
    await takeOver(page);
    await expect(kbdButton(page)).toBeHidden();
  });

  test('символы, Backspace и Enter уходят в экран, поле стирается, вне ручного режима ввод игнорируется', async ({ page }) => {
    await page.setViewportSize({ width: 600, height: 900 });
    await openBrowser(page, '&browser=hold');
    await waitLive(page);
    await takeOver(page);
    const button = kbdButton(page);
    await expect(button).toBeVisible();
    const box = await button.boundingBox();
    expect(box.width).toBeGreaterThanOrEqual(44);
    expect(box.height).toBeGreaterThanOrEqual(44);

    await button.click();
    const input = page.locator('#br-kbd-input');
    await expect(input).toBeFocused();
    await expect(page.locator('#br-stage')).toHaveClass(/is-capturing/);
    await page.keyboard.type('Ab1');
    await page.keyboard.press('Backspace');
    await page.keyboard.press('Enter');
    await page.keyboard.type('ж');
    expect(await keyboardLog(page)).toEqual(['char', 'char', 'char', 'Backspace', 'Enter', 'char']);
    expect((await screenMock(page)).keys).toBe(6);
    await expect(input).toHaveValue('');

    // клавиатуры Android шлют Backspace и Enter без имени клавиши: ловим по типу ввода
    await page.evaluate(() => {
      const field = document.getElementById('br-kbd-input');
      field.dispatchEvent(new InputEvent('beforeinput', { inputType: 'deleteContentBackward', bubbles: true, cancelable: true }));
      field.dispatchEvent(new InputEvent('beforeinput', { inputType: 'insertLineBreak', bubbles: true, cancelable: true }));
    });
    expect((await keyboardLog(page)).slice(6)).toEqual(['Backspace', 'Enter']);

    // возврат боту: поле отпущено, ввод больше не идёт
    await backBtn(page).click();
    await expect(title(page)).toHaveText('Возврат боту');
    await expect(input).not.toBeFocused();
    await expect(page.locator('#br-stage')).not.toHaveClass(/is-capturing/);
    await page.evaluate(() => document.getElementById('br-kbd-input').focus());
    await page.keyboard.type('x');
    await page.keyboard.press('Backspace');
    expect((await screenMock(page)).keys).toBe(8);
    await expect(input).toHaveValue('');
  });
});

test.describe('10.7 лист пароля', () => {
  async function openSecret(page) {
    await openBrowser(page);
    await waitLive(page);
    await takeOver(page);
    await page.getByRole('button', { name: 'Пароль скрыто', exact: true }).click();
    const dialog = page.getByRole('dialog', { name: 'Пароль для сайта' });
    await expect(dialog).toBeVisible();
    return dialog;
  }

  test('у «Что это значит» шеврон: свёрнут и раскрыт', async ({ page }) => {
    const dialog = await openSecret(page);
    const chevron = dialog.locator('.br-details summary .br-chev');
    await expect(chevron).toBeVisible();
    await expect(chevron).toHaveAttribute('aria-hidden', 'true');
    expect(await dialog.locator('.br-details summary').evaluate((el) => getComputedStyle(el).listStyleType)).toBe('none');
    const transform = () => chevron.evaluate((el) => getComputedStyle(el).transform);
    await expect.poll(transform).toBe('none');
    await dialog.getByText('Что это значит').click();
    await expect(dialog.locator('.br-details')).toHaveAttribute('open', '');
    await expect.poll(transform).not.toBe('none');
    await dialog.getByText('Что это значит').click();
    await expect.poll(transform).toBe('none');
  });

  test('подпись про имя стоит у поля имени, а не у переключателя', async ({ page }) => {
    const dialog = await openSecret(page);
    const save = dialog.getByRole('switch', { name: /Сохранить в хранилище секретов/ });
    await expect(dialog.locator('.br-switch')).not.toContainText('под этим именем');
    await expect(dialog.locator('#sec-name-wrap')).toBeHidden();
    await save.click();
    const wrap = dialog.locator('#sec-name-wrap');
    await expect(wrap).toBeVisible();
    await expect(wrap.getByLabel('Имя секрета')).toBeVisible();
    await expect(wrap.locator('#sec-name-note')).toContainText('Секрет сохранится под этим именем');
    // после ошибки проверки подпись возвращается на место
    await dialog.getByLabel('Пароль', { exact: true }).fill(SECRET);
    await wrap.getByLabel('Имя секрета').fill('1плохое имя');
    await dialog.getByRole('button', { name: 'Ввести', exact: true }).click();
    await expect(wrap.locator('#sec-name-note')).toHaveClass(/is-error/);
    await wrap.getByLabel('Имя секрета').fill('site_password');
    await dialog.getByRole('button', { name: 'Ввести', exact: true }).click();
    await expect(dialog).toHaveCount(0);
  });

  test('role=switch оформлен переключателем', async ({ page }) => {
    const dialog = await openSecret(page);
    const save = dialog.getByRole('switch', { name: /Сохранить в хранилище секретов/ });
    const look = () => save.evaluate((el) => {
      const s = getComputedStyle(el);
      return { appearance: s.appearance, width: s.width, height: s.height, background: s.backgroundColor, thumb: getComputedStyle(el, '::after').transform };
    });
    const off = await look();
    expect(off.appearance).toBe('none');
    expect(off.width).toBe('44px');
    expect(off.height).toBe('26px');
    expect(off.thumb).toBe('none');
    const row = await dialog.locator('.br-switch').boundingBox();
    expect(row.height).toBeGreaterThanOrEqual(44);
    await save.click();
    await expect(save).toBeChecked();
    await expect.poll(async () => (await look()).thumb).toBe('matrix(1, 0, 0, 1, 18, 0)'); // ползунок уехал вправо
    const on = await look();
    expect(on.background).not.toBe(off.background);
  });
});

test.describe('10.8 мелочи', () => {
  test('«Пароль скрыто» вместо «Ввести пароль скрыто»', async ({ page }) => {
    await openBrowser(page);
    await waitLive(page);
    await expect(page.getByRole('button', { name: 'Пароль скрыто', exact: true })).toHaveCount(0); // у бота кнопки нет
    await takeOver(page);
    await expect(page.getByRole('button', { name: 'Пароль скрыто', exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Ввести пароль скрыто' })).toHaveCount(0);
  });

  test('при подключении: «Подключаемся к экрану»', async ({ page }) => {
    await page.addInitScript(() => {
      window.__overlay = [];
      new MutationObserver(() => {
        const node = document.getElementById('br-overlay');
        const text = node ? node.textContent.trim() : '';
        if (text && !window.__overlay.includes(text)) window.__overlay.push(text);
      }).observe(document, { subtree: true, childList: true, characterData: true });
    });
    await openBrowser(page);
    await waitLive(page);
    const seen = await page.evaluate(() => window.__overlay);
    expect(seen).toContain('Подключаемся к экрану');
    expect(seen).not.toContain('Подключаюсь к экрану');
  });

  test('метка «только чтение» только вне ручного режима', async ({ page }) => {
    await openBrowser(page, '&browser=hold');
    await waitLive(page);
    const tag = page.locator('#br-addr-tag');
    const group = page.locator('#br-addr-group');
    await expect(tag).toBeVisible();
    await expect(tag).toHaveText('только чтение');
    await expect(group).toHaveAttribute('aria-label', 'Адрес страницы, только чтение');
    await takeOver(page);
    await expect(tag).toBeHidden();
    await expect(group).toHaveAttribute('aria-label', 'Адрес страницы');
    await expect(page.locator('#br-addr')).toBeVisible();
    await backBtn(page).click();
    await expect(title(page)).toHaveText('Возврат боту');
    await expect(tag).toBeVisible();
  });
});
