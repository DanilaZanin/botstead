import { expect, test } from '@playwright/test';

// Процедуры (docs/contracts.md §14): список, сохранение из turn, экран процедуры и правка шагов, запуск, выполнение.
// Мок-режим: ?mock=1 (вошёл админ). &procedures=none|fail|slow (нет процедур, список не загрузился с первого раза, долгая загрузка),
// &replay=off (запуск отвечает 501 not_implemented), &runs=manual (запуск не идёт сам: статус задаёт window.__procMock.setRun;
// добавлен идущий запуск run4 без треда у pr1), &secrets=off (список секретов недоступен), &actions=turn (в треде Скаута
// завершённый turn с действиями браузера). Вызовы записи: window.__procedureCalls {name, id, body}.
// Данные: pr1 «Проверка входящих в Gmail» (готова, 6 шагов, 2 с подтверждением), pr2 «Отклик на вакансию» (черновик,
// один шаг нужно заполнить), pr3 «Старая проверка статуса» (в архиве). Мок живёт в памяти страницы: перезагрузка сбрасывает его.

const isMobile = (testInfo) => testInfo.project.name === 'mobile-chromium';
const YOU = /(^|[^а-яё])(ты|тебя|тебе|тобой|твой|твоя|твоё|твои|твоих)(?![а-яё])|(Опиши|Выбери|Попробуй|Открой|Нажми|Введи|Смотри)(?![а-яё])/i;

async function openList(page, query = '') {
  await page.goto(`/?mock=1${query}#/procedures`);
  await expect(page.getByRole('heading', { name: 'Процедуры', level: 1 })).toBeVisible();
}
async function openProc(page, id, query = '') {
  await page.goto(`/?mock=1${query}#/procedures/${id}`);
  await expect(page.locator('#pr-root')).toBeVisible();
  await expect(page.locator('#pr-root h2').first()).toBeVisible();
}
const steps = (page) => page.locator('#pr-steps > li.proc-step');
const stepTitles = async (page) => (await steps(page).locator('.proc-step-title').allInnerTexts()).map((t) => t.replace(/^Шаг \d+:\s*/, '').trim());
const dialog = (page) => page.getByRole('dialog');
// Основная область экрана: на телефоне .thread-body и нижняя панель, на Mac .desktop-thread-body (боковая панель не наша).
const MAIN_CONTROLS = ':is(.thread-body, .action-bar, .desktop-thread-body) :is(a, button, select, input:not([type="checkbox"]))';
const tooSmall = (page, scope = MAIN_CONTROLS) => page.locator(scope).evaluateAll((els) => els
  .filter((el) => el.getBoundingClientRect().width > 0)
  .map((el) => ({ text: (el.innerText || el.getAttribute('aria-label') || el.id).slice(0, 40), h: Math.round(el.getBoundingClientRect().height) }))
  .filter((x) => x.h < 44));
const calls = (page, name) => page.evaluate((n) => (window.__procedureCalls || []).filter((c) => c.name === n), name);
const runButton = (page) => page.getByRole('button', { name: 'Запустить', exact: true });
async function editStep(page, n) {
  await page.getByRole('button', { name: `Шаг ${n}: изменить` }).click();
  await expect(dialog(page)).toBeVisible();
}
async function startRun(page, email = 'admin@example.org') {
  await runButton(page).click();
  const dlg = dialog(page);
  await expect(dlg.getByRole('heading', { name: 'Запустить процедуру' })).toBeVisible();
  await dlg.getByLabel(/^email/).fill(email);
  await dlg.getByRole('button', { name: 'Запустить' }).click();
  await expect(page).toHaveURL(/#\/procedure-runs\/run\d+$/);
}

test.describe('список процедур', () => {
  test('имя, бот, источник, шаги, статус, последний запуск и итог', async ({ page }) => {
    await openList(page);
    const rows = page.locator('.proc-row');
    await expect(rows).toHaveCount(3);
    const ready = rows.filter({ hasText: 'Проверка входящих в Gmail' });
    await expect(ready).toContainText('Скаут · по показу человека');
    await expect(ready).toContainText('Готова');
    await expect(ready).toContainText('6 шагов · 2 с подтверждением');
    await expect(ready.locator('[data-last-run]')).toContainText(/Запуск .+ · Готово/);
    const draft = rows.filter({ hasText: 'Отклик на вакансию' });
    await expect(draft).toContainText('Скаут · из действий бота');
    await expect(draft).toContainText('Черновик: 4 шага');
    await expect(draft).toContainText('Нужно заполнить: 1');
    await expect(draft.locator('[data-last-run]')).toHaveText('Не запускалась');
    const archived = rows.filter({ hasText: 'Старая проверка статуса' });
    await expect(archived).toContainText('SRE · импорт');
    await expect(archived).toContainText('В архиве');
    await expect(archived.locator('[data-last-run]')).toContainText(/Запуск .+ · Не удалось/);
    await expect(page.getByRole('heading', { name: 'В архиве', level: 2 })).toBeVisible();
  });

  test('строка открывает процедуру, вход есть из раздела «Рутины»', async ({ page }) => {
    await page.goto('/?mock=1#/routines');
    await page.getByRole('link', { name: /Процедуры/ }).click();
    await expect(page).toHaveURL(/#\/procedures$/);
    await page.locator('.proc-row', { hasText: 'Проверка входящих в Gmail' }).click();
    await expect(page).toHaveURL(/#\/procedures\/pr1$/);
    await expect(page.locator('#pr-root h2').first()).toHaveText('Проверка входящих в Gmail');
  });

  test('пусто: что это и как получить', async ({ page }) => {
    await openList(page, '&procedures=none');
    await expect(page.getByRole('heading', { name: 'Процедур пока нет' })).toBeVisible();
    await expect(page.getByText(/записанные шаги в браузере бота/)).toBeVisible();
    await expect(page.getByText(/Сохранить как процедуру/).first()).toBeVisible();
    await expect(page.getByRole('button', { name: 'Импорт из файла' }).first()).toBeVisible();
  });

  test('загрузка, ошибка и повтор', async ({ page }) => {
    await page.goto('/?mock=1&procedures=slow#/procedures');
    await expect(page.getByRole('status').filter({ hasText: 'Загружаю процедуры' })).toBeVisible();
    await expect(page.locator('.proc-row')).toHaveCount(3);
    await page.goto('/?mock=1&procedures=fail&x=1#/procedures');
    await expect(page.getByRole('alert').filter({ hasText: 'Процедуры не загрузились' })).toBeVisible();
    await page.getByRole('button', { name: 'Повторить' }).click();
    await expect(page.locator('.proc-row')).toHaveCount(3);
  });

  test('касание не меньше 44 px и нет горизонтальной прокрутки', async ({ page }) => {
    await openList(page);
    await expect(page.locator('.proc-row').first()).toBeVisible();
    expect(await tooSmall(page)).toEqual([]);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  });
});

test.describe('сохранение из turn', () => {
  test('кнопка только у завершённого turn с действиями; имя; 409; черновик', async ({ page }) => {
    await page.goto('/?mock=1&actions=turn#/threads/t-scout');
    const offer = page.getByRole('button', { name: 'Сохранить как процедуру' });
    await expect(offer).toBeVisible();
    await expect(offer).toHaveCount(1);
    await offer.click();
    const dlg = dialog(page);
    await expect(dlg.getByRole('heading', { name: 'Сохранить как процедуру' })).toBeVisible();
    await dlg.getByRole('button', { name: 'Сохранить', exact: true }).click();
    await expect(dlg.getByText('Введите название')).toBeVisible();
    const name = dlg.getByLabel('Название процедуры');
    await name.fill('Отклик на вакансию');
    await dlg.getByRole('button', { name: 'Сохранить', exact: true }).click();
    await expect(dlg.getByText('Процедура с таким названием уже есть')).toBeVisible();
    await expect(name).toHaveAttribute('aria-invalid', 'true');
    await expect(name).toHaveValue('Отклик на вакансию');
    await name.fill('Отклик на вакансию, копия');
    await dlg.getByRole('button', { name: 'Сохранить', exact: true }).click();
    await expect(page).toHaveURL(/#\/procedures\/pr\d+$/);
    const body = (await calls(page, 'from-turn')).at(-1).body;
    expect(body).toMatchObject({ thread_id: 't-scout', turn_id: 'tu-form', name: 'Отклик на вакансию, копия' });
    await expect(page.locator('#pr-root h2').first()).toHaveText('Отклик на вакансию, копия');
    await expect(page.locator('#pr-root').getByText('Черновик', { exact: true }).first()).toBeVisible();
    // Чтение страницы (snapshot) шагом не стало: открыть, нажать, ввести, нажать.
    expect(await stepTitles(page)).toEqual(['Открыть jobs.example.eu/812', 'Нажать кнопку «Откликнуться»', 'Ввести значение', 'Нажать кнопку «Отправить отклик»']);
    await expect(steps(page).nth(2)).toContainText('Нужно заполнить');
    await expect(steps(page).nth(3)).toContainText('Отправка · подтверждение');
    await expect(runButton(page)).toBeDisabled();
    await expect(page.locator('[data-run-reason]')).toContainText('Это черновик: заполните значения в шагах (1)');
  });

  test('без действий браузера или без завершения кнопки нет', async ({ page }) => {
    await page.goto('/?mock=1#/threads/t-scout');
    await expect(page.getByText('Нашёл 7 вакансий SRE')).toBeVisible();
    await expect(page.getByRole('button', { name: 'Сохранить как процедуру' })).toHaveCount(0);
    // У SRE есть действия браузера, но turn не завершён событием status или usage с turn_id.
    await page.goto('/?mock=1#/threads/t-sre');
    await expect(page.getByText('Причина почти наверняка в памяти')).toBeVisible();
    await expect(page.getByRole('button', { name: 'Сохранить как процедуру' })).toHaveCount(0);
  });
});

test.describe('экран процедуры', () => {
  test('шапка, параметры, шаги по-человечески, риск, повтор; секретов в разметке нет', async ({ page }) => {
    await openProc(page, 'pr1');
    const root = page.locator('#pr-root');
    await expect(root.locator('h2').first()).toHaveText('Проверка входящих в Gmail');
    await expect(root).toContainText('Входит в почту и открывает папку входящих.');
    await expect(root).toContainText('Готова');
    await expect(root.locator('.proc-kv', { hasText: 'Бот' })).toContainText('Скаут');
    await expect(root.locator('.proc-kv', { hasText: 'Версия' })).toContainText('3');
    await expect(root.locator('.proc-kv', { hasText: 'Источник' })).toContainText('по показу человека');
    const params = root.locator('.proc-param');
    await expect(params).toHaveCount(2);
    await expect(params.nth(0)).toContainText('email');
    await expect(params.nth(0)).toContainText('Текст · обязательный');
    await expect(params.nth(1)).toContainText('folder');
    await expect(params.nth(1)).toContainText('необязательный · по умолчанию: Входящие');
    expect(await stepTitles(page)).toEqual([
      'Открыть mail.google.com', 'Ввести {{email}}', 'Нажать кнопку «Далее»', 'Пароль из секрета «gmail»', 'Нажать кнопку «Войти»', 'Нажать ссылку «{{folder}}»',
    ]);
    await expect(steps(page).nth(1)).toContainText('Поле ввода «Email»');
    await expect(steps(page).nth(1)).toContainText('Получилось: виден кнопка «Далее»');
    await expect(steps(page).nth(2)).toContainText('Было: адрес подходит под ^https://accounts');
    await expect(steps(page).nth(4)).toContainText('Получилось: адрес подходит под ^https://mail');
    await expect(steps(page).nth(3).locator('[data-risk="login"]')).toHaveText('Вход · подтверждение');
    await expect(steps(page).nth(4).locator('[data-risk="login"]')).toBeVisible();
    await expect(steps(page).nth(0).locator('[data-retry]')).toHaveText('Можно повторить');
    await expect(steps(page).nth(2).locator('[data-risk], [data-retry]')).toHaveCount(0);
    // Секретов в DOM нет: ни ссылки vault:, ни поля пароля, ни значений.
    expect(await page.evaluate(() => document.getElementById('app').innerHTML.includes('vault:'))).toBe(false);
    await expect(page.locator('input[type="password"]')).toHaveCount(0);
  });

  test('телефон: шаги и кнопки не меньше 44 px, без горизонтальной прокрутки, у кнопок есть имена', async ({ page }) => {
    await openProc(page, 'pr1');
    await expect(steps(page)).toHaveCount(6);
    expect(await tooSmall(page)).toEqual([]);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    const nameless = await page.locator('#pr-root button').evaluateAll((els) => els.filter((el) => !(el.innerText || el.getAttribute('aria-label') || '').trim()).length);
    expect(nameless).toBe(0);
    await expect(page.getByRole('button', { name: 'Шаг 3: изменить' })).toBeVisible();
  });

  test('безличные формулировки без обращения на «ты»', async ({ page }) => {
    for (const hash of ['#/procedures', '#/procedures/pr1', '#/procedures/pr2']) {
      await page.goto(`/?mock=1&runs=manual&x=${encodeURIComponent(hash)}${hash}`);
      await expect.poll(async () => (await page.locator('#app').innerText()).length, { message: hash }).toBeGreaterThan(80);
      const text = await page.locator('#app').innerText();
      expect(text.match(YOU)?.[0], hash).toBeUndefined();
    }
    await page.goto('/?mock=1&runs=manual#/procedure-runs/run4');
    await expect(page.getByText('Выполняется', { exact: true }).first()).toBeVisible();
    expect((await page.locator('#app').innerText()).match(YOU)?.[0]).toBeUndefined();
  });

  test('правка шага: 422 у поля по detail, введённое не теряется', async ({ page }) => {
    await openProc(page, 'pr1');
    await editStep(page, 2);
    const dlg = dialog(page);
    await expect(dlg.getByRole('heading', { name: 'Шаг 2' })).toBeVisible();
    await expect(dlg.getByLabel('Действие')).toHaveValue('fill');
    await expect(dlg.getByRole('radio', { name: 'Параметр' })).toHaveAttribute('aria-checked', 'true');
    await dlg.getByRole('radio', { name: 'Значение' }).click();
    const value = dlg.getByLabel('Значение', { exact: true });
    await value.fill('Привет, {{nope}}');
    await dlg.getByRole('button', { name: 'Сохранить шаг' }).click();
    await expect(dlg).toBeVisible();
    await expect(dlg.locator('#sf-value-note')).toContainText('параметр не описан в параметрах процедуры');
    await expect(dlg.locator('#sf-value-note')).not.toContainText('param_undeclared');
    await expect(value).toHaveAttribute('aria-invalid', 'true');
    await expect(value).toBeFocused();
    await expect(value).toHaveValue('Привет, {{nope}}');
    // Исправление: ошибка снимается, шаг сохраняется, версия растёт.
    await value.fill('Привет, {{email}}');
    await dlg.getByRole('button', { name: 'Сохранить шаг' }).click();
    await expect(dialog(page)).toHaveCount(0);
    expect(await stepTitles(page)).toContain('Ввести «Привет, {{email}}»');
    await expect(page.locator('.proc-kv', { hasText: 'Версия' })).toContainText('4');
    const patches = await calls(page, 'patch');
    expect(patches).toHaveLength(2);
    expect(patches[1].body.steps[1]).toMatchObject({ id: 's2', action: 'fill', value: 'Привет, {{email}}', secret_ref: null });
    expect(patches[1].body.steps[1]).not.toHaveProperty('computed_risk');
  });

  test('форма шага: обязательные поля проверяются до отправки', async ({ page }) => {
    await openProc(page, 'pr1');
    await editStep(page, 3);
    const dlg = dialog(page);
    await dlg.getByLabel('Подпись элемента', { exact: true }).fill('');
    await dlg.getByRole('button', { name: 'Сохранить шаг' }).click();
    await expect(dlg.locator('#sf-name-note')).toContainText('Укажите подпись элемента');
    expect(await calls(page, 'patch')).toHaveLength(0);
    // «Дополнительно» хранит селектор «по коду»; с селектором подпись не нужна.
    await dlg.locator('.proc-advanced summary').click();
    await dlg.getByLabel(/Селектор CSS/).fill('#next');
    await dlg.getByRole('button', { name: 'Сохранить шаг' }).click();
    await expect(dialog(page)).toHaveCount(0);
    expect((await stepTitles(page))[2]).toBe('Нажать элемент по коду');
  });

  test('условия «Было» и «Получилось» сохраняются; плохой шаблон отклоняет ядро у своего поля', async ({ page }) => {
    await openProc(page, 'pr1');
    await editStep(page, 3);
    const dlg = dialog(page);
    await dlg.getByLabel('Адрес страницы подходит под шаблон').nth(1).fill('(');
    await dlg.getByRole('button', { name: 'Сохранить шаг' }).click();
    await expect(dlg.locator('#sf-exp-url-note')).toContainText('не похоже на регулярное выражение');
    await dlg.getByLabel('Адрес страницы подходит под шаблон').nth(1).fill('^https://mail');
    await dlg.getByLabel('На странице есть текст').fill('Входящие');
    await dlg.getByRole('button', { name: 'Сохранить шаг' }).click();
    await expect(dialog(page)).toHaveCount(0);
    await expect(steps(page).nth(2)).toContainText('Получилось: адрес подходит под ^https://mail, есть текст «Входящие»');
  });

  test('риск только вверх: ниже вычисленного недоступно, повышение сохраняется', async ({ page }) => {
    await openProc(page, 'pr1');
    await editStep(page, 5);
    const dlg = dialog(page);
    const risk = dlg.getByLabel('Подтверждение');
    await expect(risk).toHaveValue('login');
    await expect(dlg.locator('#sf-risk-note')).toContainText('Сервер вычислил «Вход». Ниже этого уровня риск понизить нельзя.');
    await expect(risk.locator('option[value="none"]')).toBeDisabled();
    await expect(risk.locator('option[value="other"]')).toBeDisabled();
    // Порядок ядра: send < push < exec < delete < login < pay, поэтому у шага «Вход» ниже доступны только login и pay.
    for (const lower of ['send', 'push', 'exec', 'delete']) await expect(risk.locator(`option[value="${lower}"]`)).toBeDisabled();
    await expect(risk.locator('option[value="login"]')).toBeEnabled();
    await expect(risk.locator('option[value="pay"]')).toBeEnabled();
    await risk.selectOption('pay');
    await dlg.getByRole('button', { name: 'Сохранить шаг' }).click();
    await expect(dialog(page)).toHaveCount(0);
    await expect(steps(page).nth(4).locator('[data-risk="pay"]')).toHaveText('Оплата · подтверждение');
    // Шаг без риска: все уровни доступны.
    await editStep(page, 3);
    await expect(dialog(page).getByLabel('Подтверждение').locator('option[disabled]')).toHaveCount(0);
  });

  test('если понизить риск в обход формы, ядро отвечает 422 у поля риска, введённое остаётся', async ({ page }) => {
    await openProc(page, 'pr1');
    await editStep(page, 5);
    const dlg = dialog(page);
    await dlg.getByLabel('Подтверждение').evaluate((select) => { select.querySelector('option[value="none"]').disabled = false; });
    await dlg.getByLabel('Подтверждение').selectOption('none');
    await dlg.getByRole('button', { name: 'Сохранить шаг' }).click();
    await expect(dlg.locator('#sf-risk-note')).toContainText('риск ниже вычисленного');
    await expect(dlg.getByLabel('Подтверждение')).toHaveAttribute('aria-invalid', 'true');
    await expect(dlg.getByLabel('Подпись элемента', { exact: true })).toHaveValue('Войти');
    await expect(steps(page).nth(4).locator('[data-risk="login"]')).toBeVisible();
  });

  test('пароль только секретом: выбор из хранилища, ввода нет, риск поднимает ядро', async ({ page }) => {
    await openProc(page, 'pr1');
    await page.getByRole('button', { name: 'Шаг', exact: true }).click();
    const dlg = dialog(page);
    await dlg.getByLabel('Действие').selectOption('fill');
    await dlg.getByLabel('Что найти на странице').selectOption('textbox');
    await dlg.getByLabel('Подпись элемента', { exact: true }).fill('Пароль');
    await dlg.getByRole('radio', { name: 'Секрет' }).click();
    const secret = dlg.getByLabel('Секрет из хранилища');
    await expect(secret.locator('option')).toHaveText(['Выберите секрет', 'gmail', 'jobs-site']);
    await expect(dlg.locator('input[type="password"]')).toHaveCount(0);
    await expect(dlg.getByLabel('Значение', { exact: true })).toBeHidden();
    await dlg.getByRole('button', { name: 'Сохранить шаг' }).click();
    await expect(dlg.locator('#sf-secret-note')).toContainText('Выберите секрет');
    await secret.selectOption('jobs-site');
    await dlg.getByRole('button', { name: 'Сохранить шаг' }).click();
    await expect(dialog(page)).toHaveCount(0);
    const last = steps(page).last();
    await expect(last.locator('.proc-step-title')).toContainText('Пароль из секрета «jobs-site»');
    await expect(last.locator('[data-risk="login"]')).toBeVisible();
    const sent = (await calls(page, 'patch')).at(-1).body.steps.at(-1);
    expect(sent).toMatchObject({ value: null, secret_ref: 'vault:jobs-site' });
    expect(sent).not.toHaveProperty('risk');  // риск не тронут: его вычисляет ядро
    expect(await page.evaluate(() => document.getElementById('app').innerHTML.includes('vault:'))).toBe(false);
  });

  test('ошибки ядра приходят кодом и переводятся: поле карты, регулярное выражение, параметр в селекторе', async ({ page }) => {
    await openProc(page, 'pr1');
    await editStep(page, 2);
    let dlg = dialog(page);
    await dlg.getByLabel('Подпись элемента', { exact: true }).fill('CVV');
    await dlg.getByRole('radio', { name: 'Значение' }).click();
    await dlg.getByLabel('Значение', { exact: true }).fill('123');
    await dlg.getByRole('button', { name: 'Сохранить шаг' }).click();
    await expect(dlg.locator('#sf-value-note')).toContainText('нужен секрет, а не значение');
    await expect(dlg.locator('#sf-value-note')).not.toContainText('secret_required');
    await expect(dlg.getByLabel('Значение', { exact: true })).toHaveValue('123');
    await dlg.getByRole('button', { name: 'Отмена' }).click();

    await editStep(page, 3);
    dlg = dialog(page);
    await dlg.getByLabel('Адрес страницы подходит под шаблон').nth(1).fill('(a+)+$');
    await dlg.getByRole('button', { name: 'Сохранить шаг' }).click();
    await expect(dlg.locator('#sf-exp-url-note')).toContainText('повтор внутри повторяющейся группы');
    await dlg.getByLabel('Адрес страницы подходит под шаблон').nth(1).fill('');
    await dlg.getByLabel('Подпись элемента', { exact: true }).fill('');
    await dlg.locator('.proc-advanced summary').click();
    await dlg.getByLabel(/Селектор CSS/).fill('#{{email}}');
    await dlg.getByRole('button', { name: 'Сохранить шаг' }).click();
    await expect(dlg.locator('#sf-selector-note')).toContainText('в селекторе параметры не работают');
    // Три попытки ушли на сервер и все отклонены (мок пишет в журнал и отклонённые вызовы): лист открыт, ничего не сохранено.
    expect(await calls(page, 'patch')).toHaveLength(3);
    await expect(dlg).toBeVisible();
  });

  test('черновик из действий бота: безымянное поле открывается сразу на «Секрет», после выбора метки needs_* уходят', async ({ page }) => {
    await page.goto('/?mock=1&actions=turn#/threads/t-scout');
    await page.getByRole('button', { name: 'Сохранить как процедуру' }).click();
    await dialog(page).getByLabel('Название процедуры').fill('С секретным полем');
    await dialog(page).getByRole('button', { name: 'Сохранить', exact: true }).click();
    await expect(page).toHaveURL(/#\/procedures\/pr\d+$/);
    await editStep(page, 3);
    const dlg = dialog(page);
    await expect(dlg.getByRole('radio', { name: 'Секрет' })).toHaveAttribute('aria-checked', 'true');
    await dlg.getByLabel('Подпись элемента', { exact: true }).fill('Телефон');  // подпись при записи не сохранилась: форма просит назвать поле
    await dlg.getByLabel('Секрет из хранилища').selectOption('jobs-site');
    await dlg.getByRole('button', { name: 'Сохранить шаг' }).click();
    await expect(dialog(page)).toHaveCount(0);
    const sent = (await calls(page, 'patch')).at(-1).body.steps[2];
    expect(sent).toMatchObject({ value: null, secret_ref: 'vault:jobs-site' });
    expect(sent).not.toHaveProperty('needs_value');
    expect(sent).not.toHaveProperty('needs_secret');
    expect(sent).not.toHaveProperty('computed_risk');
  });

  test('список секретов недоступен: вводится только имя секрета', async ({ page }) => {
    await openProc(page, 'pr1', '&secrets=off');
    await editStep(page, 4);
    const dlg = dialog(page);
    await expect(dlg.getByRole('radio', { name: 'Секрет' })).toHaveAttribute('aria-checked', 'true');
    const name = dlg.getByLabel('Имя секрета');
    await expect(name).toHaveValue('gmail');
    await expect(dlg.getByText('Сам пароль вводить не нужно')).toBeVisible();
    await expect(dlg.locator('input[type="password"]')).toHaveCount(0);
  });

  test('порядок с клавиатуры: Enter на «выше» и Alt со стрелкой, фокус остаётся на шаге', async ({ page }) => {
    await openProc(page, 'pr1');
    const before = await stepTitles(page);
    await page.getByRole('button', { name: 'Шаг 2: выше' }).focus();
    await page.keyboard.press('Enter');
    let now = await stepTitles(page);
    expect(now.slice(0, 2)).toEqual([before[1], before[0]]);
    await expect(page.getByRole('button', { name: 'Шаг 1: выше' })).toBeFocused();
    await expect(page.getByRole('button', { name: 'Шаг 1: выше' })).toHaveAttribute('aria-disabled', 'true');
    await expect(page.locator('#pr-live')).toContainText('теперь 1 из 6');
    // Alt и стрелка вниз на фокусе внутри шага двигают тот же шаг дальше.
    await page.keyboard.press('Alt+ArrowDown');
    now = await stepTitles(page);
    expect(now.slice(0, 3)).toEqual([before[0], before[1], before[2]]);
    await page.keyboard.press('Alt+ArrowDown');
    now = await stepTitles(page);
    expect(now.slice(0, 3)).toEqual([before[0], before[2], before[1]]);
    await expect(page.locator('#pr-live')).toContainText('теперь 3 из 6');
    await expect(page.getByRole('button', { name: 'Шаг 3: ниже' })).toBeFocused();
    // Последний шаг вниз не идёт.
    await page.getByRole('button', { name: 'Шаг 6: ниже' }).focus();
    await expect(page.getByRole('button', { name: 'Шаг 6: ниже' })).toHaveAttribute('aria-disabled', 'true');
    await page.keyboard.press('Enter');
    expect(await stepTitles(page)).toEqual(now);
    // Порядок ушёл в ядро полным списком id, по одному запросу на перестановку.
    await expect.poll(async () => (await calls(page, 'patch')).length).toBe(3);
    const last = (await calls(page, 'patch')).at(-1).body.steps.map((s) => s.id);
    expect(last).toEqual(['s1', 's3', 's2', 's4', 's5', 's6']);
  });

  test('удаление и добавление шага', async ({ page }) => {
    await openProc(page, 'pr1');
    await page.getByRole('button', { name: 'Шаг 6: удалить' }).click();
    const dlg = dialog(page);
    await expect(dlg.getByRole('heading', { name: 'Удалить шаг?' })).toBeVisible();
    await expect(dlg.getByRole('button', { name: 'Отмена' })).toBeFocused();
    await dlg.getByRole('button', { name: 'Удалить шаг' }).click();
    await expect(steps(page)).toHaveCount(5);
    await page.getByRole('button', { name: 'Шаг', exact: true }).click();
    const add = dialog(page);
    await add.getByLabel('Действие').selectOption('press');
    await add.getByLabel('Клавиша').fill('Enter');
    await add.getByRole('button', { name: 'Сохранить шаг' }).click();
    await expect(steps(page)).toHaveCount(6);
    expect((await stepTitles(page)).at(-1)).toBe('Нажать клавишу Enter');
  });

  test('параметры: добавить секретный, значения по умолчанию у него нет', async ({ page }) => {
    await openProc(page, 'pr1');
    await page.getByRole('button', { name: 'Параметр', exact: true }).click();
    const dlg = dialog(page);
    await dlg.getByRole('button', { name: 'Сохранить' }).click();
    await expect(dlg.locator('#pm-name-note')).toContainText('Введите имя параметра');
    await dlg.getByLabel('Имя').fill('pass word');
    await dlg.getByRole('button', { name: 'Сохранить' }).click();
    await expect(dlg.locator('#pm-name-note')).toContainText('нельзя использовать пробелы');
    await dlg.getByLabel('Имя').fill('mailpass');
    await dlg.getByLabel(/Секрет: при запуске/).check();
    await expect(dlg.getByLabel('Значение по умолчанию')).toBeHidden();
    await dlg.getByRole('button', { name: 'Сохранить' }).click();
    await expect(dialog(page)).toHaveCount(0);
    await expect(page.locator('.proc-param[data-param="mailpass"]')).toContainText('секрет: выбирается при запуске');
    const body = (await calls(page, 'patch')).at(-1).body;
    expect(body.params.at(-1)).toEqual({ name: 'mailpass', type: 'string', required: false, default: null, secret: true });
  });

  test('название занято: 409 у поля названия', async ({ page }) => {
    await openProc(page, 'pr1');
    await page.getByRole('button', { name: 'Название и бот' }).click();
    const dlg = dialog(page);
    const name = dlg.getByLabel('Название', { exact: true });
    await name.fill('Отклик на вакансию');
    await dlg.getByRole('button', { name: 'Сохранить' }).click();
    await expect(dlg.locator('#mt-name-note')).toContainText('Процедура с таким названием уже есть');
    await expect(name).toHaveAttribute('aria-invalid', 'true');
    await expect(name).toHaveValue('Отклик на вакансию');
    await name.fill('Почта: входящие');
    await dlg.getByRole('button', { name: 'Сохранить' }).click();
    await expect(dialog(page)).toHaveCount(0);
    await expect(page.locator('#pr-root h2').first()).toHaveText('Почта: входящие');
  });

  test('черновик становится готовым после заполнения значения', async ({ page }) => {
    await openProc(page, 'pr2');
    const ready = page.getByRole('button', { name: 'Готова к запуску' });
    await expect(ready).toBeDisabled();
    await expect(runButton(page)).toBeDisabled();
    await editStep(page, 3);
    const dlg = dialog(page);
    await dlg.getByLabel('Значение', { exact: true }).fill('+7 900 000-00-00');
    await dlg.getByRole('button', { name: 'Сохранить шаг' }).click();
    await expect(dialog(page)).toHaveCount(0);
    await expect(steps(page).nth(2)).not.toContainText('Нужно заполнить');
    await expect(ready).toBeEnabled();
    await expect(runButton(page)).toBeDisabled();
    await expect(page.locator('[data-run-reason]')).toContainText('проверьте шаги и отметьте процедуру готовой');
    await ready.click();
    await expect(page.locator('#pr-root').getByText('Готова', { exact: true }).first()).toBeVisible();
    await expect(runButton(page)).toBeEnabled();
  });

  test('архив: запуск недоступен с причиной, из архива можно вернуть', async ({ page }) => {
    await openProc(page, 'pr1');
    await page.getByRole('button', { name: 'В архив' }).click();
    await expect(page.locator('#pr-root').getByText('В архиве', { exact: true }).first()).toBeVisible();
    await expect(runButton(page)).toBeDisabled();
    await expect(page.locator('[data-run-reason]')).toContainText('Процедура в архиве');
    await page.getByRole('button', { name: 'Вернуть из архива' }).click();
    await expect(runButton(page)).toBeEnabled();
  });
});

test.describe('запуск', () => {
  test('лист: бот по умолчанию, обязательный параметр, список подтверждений, значения уходят в ядро', async ({ page }) => {
    await openProc(page, 'pr1', '&runs=manual');
    await runButton(page).click();
    const dlg = dialog(page);
    await expect(dlg.getByRole('heading', { name: 'Запустить процедуру' })).toBeVisible();
    await expect(dlg.getByLabel('Бот')).toHaveValue('scout');
    await expect(dlg.getByLabel(/^email \(обязательный\)/)).toBeVisible();
    await expect(dlg.getByLabel(/^folder \(необязательный, по умолчанию «Входящие»\)/)).toBeVisible();
    const warning = dlg.getByRole('note');
    await expect(warning).toContainText('2 шага потребуют вашего подтверждения');
    await expect(warning.locator('li')).toHaveText(['Шаг 4: Пароль из секрета «gmail» (вход)', 'Шаг 5: Нажать кнопку «Войти» (вход)']);
    await dlg.getByRole('button', { name: 'Запустить' }).click();
    await expect(dlg.locator('#rs-p-0-note')).toContainText('Укажите значение');
    await expect(dlg.getByLabel(/^email/)).toBeFocused();
    expect(await calls(page, 'run')).toHaveLength(0);
    await dlg.getByLabel(/^email/).fill('alice@example.org');
    await dlg.getByRole('button', { name: 'Запустить' }).click();
    await expect(page).toHaveURL(/#\/procedure-runs\/run\d+$/);
    const run = (await calls(page, 'run')).at(-1);
    expect(run).toMatchObject({ id: 'pr1', body: { bot_id: 'scout', params: { email: 'alice@example.org' } } });
    await expect(page.getByText('В очереди', { exact: true }).first()).toBeVisible();
  });

  test('параметр-секрет выбирается из хранилища, на экране запуска остаётся только имя', async ({ page }) => {
    await openProc(page, 'pr1', '&runs=manual');
    await page.getByRole('button', { name: 'Параметр', exact: true }).click();
    let dlg = dialog(page);
    await dlg.getByLabel('Имя').fill('mailpass');
    await dlg.getByLabel(/Секрет: при запуске/).check();
    await dlg.getByLabel(/Обязательный/).check();
    await dlg.getByRole('button', { name: 'Сохранить' }).click();
    await expect(dialog(page)).toHaveCount(0);
    await runButton(page).click();
    dlg = dialog(page);
    await dlg.getByLabel(/^email/).fill('a@b.c');
    await dlg.getByRole('button', { name: 'Запустить' }).click();
    await expect(dlg.locator('#rs-p-2-note')).toContainText('Выберите секрет');
    await expect(dlg.locator('input[type="password"]')).toHaveCount(0);
    const secret = dlg.getByLabel(/^mailpass \(секрет, обязательный\)/);
    await expect(secret.locator('option')).toHaveText(['Выберите секрет', 'gmail', 'jobs-site']);
    await secret.selectOption({ label: 'gmail' });
    await dlg.getByRole('button', { name: 'Запустить' }).click();
    await expect(page).toHaveURL(/#\/procedure-runs\/run\d+$/);
    expect((await calls(page, 'run')).at(-1).body.params).toEqual({ email: 'a@b.c', mailpass: 'vault:gmail' });
    await expect(page.locator('.proc-kv', { hasText: 'mailpass' })).toContainText('секрет «gmail»');
    expect(await page.evaluate(() => document.getElementById('app').innerHTML.includes('vault:'))).toBe(false);
  });

  test('ядро отклонило запуск: 400 у поля параметра', async ({ page }) => {
    await openProc(page, 'pr1', '&runs=manual');
    await runButton(page).click();
    const dlg = dialog(page);
    await dlg.getByLabel(/^email/).fill('a@b.c');
    // Бот снят со списка: ядро отвечает 400 invalid, поле бота получает текст.
    await dlg.getByLabel('Бот').evaluate((select) => { select.insertAdjacentHTML('beforeend', '<option value="ghost">Призрак</option>'); select.value = 'ghost'; });
    await dlg.getByRole('button', { name: 'Запустить' }).click();
    await expect(dlg.locator('#rs-bot-note')).toContainText('бот не найден');
    await expect(dlg.getByLabel(/^email/)).toHaveValue('a@b.c');
  });

  test('воспроизведение не включено: 501 на запуск показан прямо, лист остаётся', async ({ page }) => {
    await openProc(page, 'pr1', '&replay=off');
    await runButton(page).click();
    const dlg = dialog(page);
    await dlg.getByLabel(/^email/).fill('a@b.c');
    await dlg.getByRole('button', { name: 'Запустить' }).click();
    const alert = dlg.getByRole('alert');
    await expect(alert).toContainText('Воспроизведение не включено');
    await expect(alert).toBeFocused();
    await expect(dlg.getByLabel(/^email/)).toHaveValue('a@b.c');
    await expect(page).toHaveURL(/#\/procedures\/pr1$/);
  });

  test('телефон: лист запуска помещается в экран, кнопки не меньше 44 px', async ({ page }) => {
    await openProc(page, 'pr1');
    await runButton(page).click();
    const dlg = dialog(page);
    await expect(dlg.getByLabel(/^email/)).toBeVisible();
    expect(await tooSmall(page, '[role="dialog"] :is(button, select, input:not([type="checkbox"]))')).toEqual([]);
    const box = await dlg.boundingBox();
    expect(box.width).toBeLessThanOrEqual(page.viewportSize().width);
    // Фокус внутри листа, после закрытия возвращается на кнопку запуска.
    expect(await page.evaluate(() => document.activeElement.closest('[role="dialog"]') !== null)).toBe(true);
    await page.keyboard.press('Escape');
    await expect(dialog(page)).toHaveCount(0);
    await expect(runButton(page)).toBeFocused();
  });
});

test.describe('выполнение', () => {
  // Запуск через лист, дальше статусы задаёт тест (runs=manual): экран следит за запуском по потоку треда.
  async function begin(page) {
    await openProc(page, 'pr1', '&runs=manual');
    await startRun(page);
    await expect(page.locator('[data-run-status="queued"]')).toContainText('В очереди');
    return page.url().split('/').pop();
  }
  const setRun = (page, id, patch) => page.evaluate(([i, p]) => window.__procMock.setRun(i, p), [id, patch]);
  const log = (ids, base = {}) => ids.map((id, i) => ({ step_id: id, status: 'ok', at: new Date().toISOString(), duration_ms: 900 + i * 400, ...base }));
  const stepState = (page, n) => page.locator('.run-step').nth(n - 1).locator('[data-step-state]');

  test('все статусы словами: идёт, подтверждение, бот разбирается, ваше решение, ошибка', async ({ page }) => {
    const id = await begin(page);
    const status = page.locator('[data-run-status]');
    for (let i = 1; i <= 6; i++) await expect(stepState(page, i)).toHaveText('Ожидает');

    await setRun(page, id, { status: 'running', next_step: 2, started_at: new Date().toISOString(), step_log: log(['s1', 's2']) });
    await expect(status).toHaveAttribute('data-run-status', 'running');
    await expect(status).toContainText('Выполняется');
    await expect(stepState(page, 1)).toHaveText('Готово');
    await expect(stepState(page, 2)).toHaveText('Готово');
    await expect(stepState(page, 3)).toHaveText('Выполняется');
    await expect(stepState(page, 4)).toHaveText('Ожидает');
    await expect(page.locator('.run-step').nth(1)).toContainText('1,3 с');
    await expect(page.getByRole('button', { name: 'Остановить' })).toBeVisible();

    await setRun(page, id, { status: 'waiting_approval', next_step: 3, step_log: log(['s1', 's2', 's3']) });
    await expect(status).toContainText('Ждёт подтверждения');
    await expect(stepState(page, 4)).toHaveText('Ждёт подтверждения');
    const approve = page.getByRole('link', { name: 'Открыть подтверждение' });
    await expect(approve).toBeVisible();
    await expect(approve).toHaveAttribute('href', /#\/(approvals|threads)\//);

    await setRun(page, id, { status: 'waiting_model' });
    await expect(status).toContainText('Бот разбирается');
    await expect(page.locator('[data-run-detail]')).toContainText('предложит правку шага');
    await expect(stepState(page, 4)).toHaveText('Бот разбирается');

    await setRun(page, id, { status: 'waiting_human' });
    await expect(status).toContainText('Ваше решение');
    await expect(page.getByRole('link', { name: 'Открыть браузер бота' })).toHaveAttribute('href', '#/bots/scout/browser');
    await expect(page.getByRole('button', { name: 'Повторить шаг' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Пропустить шаг' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Остановить' })).toBeVisible();
    await expect(stepState(page, 4)).toHaveText('Ваше решение');

    await setRun(page, id, { status: 'failed', error: 'secret_not_found', finished_at: new Date().toISOString(), step_log: [...log(['s1', 's2', 's3']), { step_id: 's4', status: 'failed', at: new Date().toISOString(), duration_ms: 200, error: 'secret_not_found' }] });
    await expect(status).toContainText('Не удалось');
    await expect(page.locator('[data-run-detail]')).toContainText('Секрет не найден');
    await expect(stepState(page, 4)).toHaveText('Ошибка');
    await expect(page.locator('.run-step').nth(3)).toContainText('Секрет не найден');
    await expect(stepState(page, 5)).toHaveText('Не выполнялся');
    await expect(page.getByRole('button', { name: 'Остановить' })).toHaveCount(0);
  });

  test('ваше решение: повторить, пропустить, остановить; каждое с подтверждением и в ядро уходит action', async ({ page }) => {
    const id = await begin(page);
    await setRun(page, id, { status: 'waiting_human', next_step: 2, step_log: log(['s1', 's2']) });
    await expect(page.locator('[data-run-status]')).toContainText('Ваше решение');
    await expect(page.getByRole('button', { name: 'Остановить', exact: true })).toHaveCount(0);

    await page.getByRole('button', { name: 'Повторить шаг' }).click();
    let dlg = dialog(page);
    await expect(dlg.getByRole('heading', { name: 'Повторить шаг?' })).toBeVisible();
    await expect(dlg.getByRole('button', { name: 'Отмена' })).toBeFocused();
    await dlg.getByRole('button', { name: 'Отмена' }).click();
    expect(await calls(page, 'decide')).toHaveLength(0);
    await page.getByRole('button', { name: 'Повторить шаг' }).click();
    await dialog(page).getByRole('button', { name: 'Повторить', exact: true }).click();
    await expect(page.locator('[data-run-status]')).toContainText('Выполняется');
    expect((await calls(page, 'decide')).at(-1)).toMatchObject({ id, body: { action: 'retry' } });

    await setRun(page, id, { status: 'waiting_human' });
    await page.getByRole('button', { name: 'Пропустить шаг' }).click();
    await expect(dialog(page).getByRole('heading', { name: 'Пропустить шаг?' })).toBeVisible();
    await dialog(page).getByRole('button', { name: 'Пропустить', exact: true }).click();
    await expect(stepState(page, 3)).toHaveText('Пропущен');
    expect((await calls(page, 'decide')).at(-1).body).toEqual({ action: 'skip' });

    await setRun(page, id, { status: 'waiting_human' });
    await page.getByRole('button', { name: 'Остановить запуск' }).click();
    dlg = dialog(page);
    await expect(dlg.getByRole('heading', { name: 'Остановить запуск?' })).toBeVisible();
    await dlg.getByRole('button', { name: 'Остановить', exact: true }).click();
    await expect(page.locator('[data-run-status]')).toContainText('Остановлен');
    expect((await calls(page, 'decide')).at(-1).body).toEqual({ action: 'stop' });
  });

  test('воспроизведение не включено: решение отвечает 501, действия остаются', async ({ page }) => {
    await page.goto('/?mock=1&runs=manual&replay=off#/procedure-runs/run4');
    await page.evaluate(() => window.__procMock.setRun('run4', { status: 'waiting_human' }));
    await expect(page.locator('[data-run-status]')).toContainText('Ваше решение', { timeout: 5000 });
    await page.getByRole('button', { name: 'Пропустить шаг' }).click();
    await dialog(page).getByRole('button', { name: 'Пропустить', exact: true }).click();
    await expect(dialog(page).getByRole('alert')).toContainText('Воспроизведение не включено');
    await dialog(page).getByRole('button', { name: 'Отмена' }).click();
    await expect(page.getByRole('button', { name: 'Пропустить шаг' })).toBeVisible();
  });

  test('шаги запуска берутся из снимка: правка процедуры после запуска их не меняет', async ({ page }) => {
    await page.goto('/?mock=1&runs=manual#/procedures/pr1');
    await expect(page.locator('#pr-root')).toBeVisible();
    await editStep(page, 3);
    const dlg = dialog(page);
    await dlg.getByLabel('Подпись элемента', { exact: true }).fill('Продолжить');
    await dlg.getByRole('button', { name: 'Сохранить шаг' }).click();
    await expect(dialog(page)).toHaveCount(0);
    expect(await stepTitles(page)).toContain('Нажать кнопку «Продолжить»');
    await page.locator('.proc-run-row').first().click();
    await expect(page).toHaveURL(/#\/procedure-runs\/run\d+$/);
    await expect(page.locator('.run-step .proc-step-title').nth(2)).toContainText('Нажать кнопку «Далее»');
    await expect(page.locator('[data-run-version-note]')).toContainText('показаны шаги версии 3');
    expect(await page.locator('.run-step .proc-step-title').allInnerTexts()).not.toContain('Шаг 3: Нажать кнопку «Продолжить»');
  });

  test('готово и пропущенный шаг', async ({ page }) => {
    const id = await begin(page);
    await setRun(page, id, {
      status: 'done', next_step: 6, finished_at: new Date().toISOString(),
      step_log: [...log(['s1', 's2', 's3']), { step_id: 's4', status: 'skipped', at: new Date().toISOString(), duration_ms: 0 }, ...log(['s5', 's6'])],
    });
    await expect(page.locator('[data-run-status]')).toContainText('Готово');
    await expect(stepState(page, 4)).toHaveText('Пропущен');
    await expect(stepState(page, 6)).toHaveText('Готово');
    await expect(page.getByRole('button', { name: 'Остановить' })).toHaveCount(0);
  });

  test('запуск без треда: статус обновляется опросом раз в 2 секунды', async ({ page }) => {
    await page.goto('/?mock=1&runs=manual#/procedure-runs/run4');
    await expect(page.locator('[data-run-status]')).toContainText('Выполняется');
    await expect(stepState(page, 1)).toHaveText('Готово');
    await setRun(page, 'run4', { status: 'waiting_model' });
    await expect(page.locator('[data-run-status]')).toContainText('Бот разбирается', { timeout: 5000 });
    await setRun(page, 'run4', { status: 'done', finished_at: new Date().toISOString(), next_step: 6 });
    await expect(page.locator('[data-run-status]')).toContainText('Готово', { timeout: 5000 });
  });

  test('автоматический запуск доходит до подтверждения и продолжается после решения', async ({ page }) => {
    await openProc(page, 'pr1');
    await startRun(page);
    await expect(page.locator('[data-run-status]')).toContainText('Ждёт подтверждения', { timeout: 15000 });
    await expect(stepState(page, 4)).toHaveText('Ждёт подтверждения');
    await page.getByRole('link', { name: 'Открыть подтверждение' }).click();
    await expect(page).toHaveURL(/#\/approvals\/apr-run\d+-s4$/);
    await page.getByRole('button', { name: 'Разрешить' }).click();
    await page.goBack();
    await expect(page.locator('[data-run-status]')).toContainText('Ждёт подтверждения', { timeout: 15000 });
    expect(await page.evaluate(() => window.__procMock.approvals.filter((a) => a.id.startsWith('apr-run')).map((a) => a.status))).toContain('approved');
  });

  test('остановка с подтверждением', async ({ page }) => {
    await page.goto('/?mock=1&runs=manual#/procedure-runs/run4');
    await page.getByRole('button', { name: 'Остановить' }).click();
    const dlg = dialog(page);
    await expect(dlg.getByRole('heading', { name: 'Остановить запуск?' })).toBeVisible();
    await expect(dlg.getByRole('button', { name: 'Отмена' })).toBeFocused();
    await dlg.getByRole('button', { name: 'Отмена' }).click();
    expect(await calls(page, 'stop')).toHaveLength(0);
    await expect(page.locator('[data-run-status]')).toContainText('Выполняется');
    await page.getByRole('button', { name: 'Остановить' }).click();
    await dialog(page).getByRole('button', { name: 'Остановить' }).click();
    await expect(page.locator('[data-run-status]')).toContainText('Остановлен');
    await expect(page.getByRole('button', { name: 'Остановить' })).toHaveCount(0);
    expect((await calls(page, 'stop')).at(-1).id).toBe('run4');
  });

  test('остановка уже завершённого запуска: 409 объяснён, экран обновляется', async ({ page }) => {
    await page.goto('/?mock=1&runs=manual#/procedure-runs/run4');
    await page.getByRole('button', { name: 'Остановить' }).click();
    const dlg = dialog(page);
    await expect(dlg.getByRole('heading', { name: 'Остановить запуск?' })).toBeVisible();
    await page.evaluate(() => { window.__procMock.runs.find((r) => r.id === 'run4').status = 'done'; });
    await dlg.getByRole('button', { name: 'Остановить' }).click();
    await expect(dlg.getByRole('alert')).toContainText('Запуск уже завершён');
  });

  test('история запусков на экране процедуры ведёт на экран выполнения', async ({ page }) => {
    await openProc(page, 'pr1');
    const rows = page.locator('.proc-run-row');
    await expect(rows).toHaveCount(1);
    await expect(rows.first()).toContainText('Готово');
    await expect(rows.first()).toContainText(/\d+(,\d)? с|мин/);
    await rows.first().click();
    await expect(page).toHaveURL(/#\/procedure-runs\/run1$/);
    await expect(page.locator('[data-run-status]')).toContainText('Готово');
    await expect(page.locator('.run-step [data-step-state]')).toHaveText(Array(6).fill('Готово'));
    await page.getByRole('link', { name: 'К процедуре' }).click();
    await expect(page).toHaveURL(/#\/procedures\/pr1$/);
  });

  test('запуск не найден', async ({ page }) => {
    await page.goto('/?mock=1#/procedure-runs/nope');
    await expect(page.getByRole('heading', { name: 'Запуск не найден' })).toBeVisible();
  });
});

test.describe('экспорт, импорт, удаление', () => {
  test('экспорт в JSON: файл с шагами, секрет ссылкой по имени', async ({ page }) => {
    await openProc(page, 'pr1');
    const download = page.waitForEvent('download');
    await page.getByRole('button', { name: 'Экспорт в JSON' }).click();
    const file = await download;
    expect(file.suggestedFilename()).toBe('Проверка-входящих-в-Gmail.procedure.json');
    const doc = JSON.parse(await (await import('node:fs/promises')).readFile(await file.path(), 'utf8'));
    expect(doc.format).toBe('bothub-procedure/1');
    expect(doc).not.toHaveProperty('version');
    expect(Object.keys(doc).sort()).toEqual(['description', 'format', 'name', 'params', 'steps']);
    expect(doc.name).toBe('Проверка входящих в Gmail');
    expect(doc.steps).toHaveLength(6);
    expect(doc.steps.some((st) => 'computed_risk' in st)).toBe(false);
    expect(doc.steps[3]).toMatchObject({ secret_ref: 'vault:gmail', value: null });
    expect(doc.params.map((p) => p.name)).toEqual(['email', 'folder']);
    expect((await calls(page, 'export')).at(-1).id).toBe('pr1');
  });

  test('импорт: битый JSON, файл без шагов, занятое название, затем успех', async ({ page }) => {
    await openList(page);
    await page.getByRole('button', { name: 'Импорт из файла' }).first().click();
    const dlg = dialog(page);
    const text = dlg.getByLabel('Или вставьте JSON');
    await dlg.getByRole('button', { name: 'Импортировать' }).click();
    await expect(dlg.locator('#im-text-note')).toContainText('Выберите файл или вставьте JSON');
    await text.fill('{"name": "Битая", ');
    await dlg.getByRole('button', { name: 'Импортировать' }).click();
    await expect(dlg.locator('#im-text-note')).toContainText('Это не JSON');
    await expect(text).toHaveAttribute('aria-invalid', 'true');
    await expect(text).toHaveValue('{"name": "Битая", ');
    expect(await calls(page, 'import')).toHaveLength(0);
    await text.fill('[1, 2]');
    await dlg.getByRole('button', { name: 'Импортировать' }).click();
    await expect(dlg.locator('#im-text-note')).toContainText('Ожидается один объект');
    await text.fill('{"name": "Без шагов"}');
    await dlg.getByRole('button', { name: 'Импортировать' }).click();
    await expect(dlg.locator('#im-text-note')).toContainText('Шаги');
    await expect(dlg.locator('#im-text-note')).toContainText('не может быть пустым');
    await expect(text).toHaveValue('{"name": "Без шагов"}');
    await text.fill(JSON.stringify({ name: 'Отклик на вакансию', steps: [{ id: 's1', action: 'navigate', target: { url: 'https://a.example' } }] }));
    await dlg.getByRole('button', { name: 'Импортировать' }).click();
    await expect(dlg.locator('#im-text-note')).toContainText('Процедура с таким названием уже есть');
    // Файл: содержимое попадает в поле, ядро принимает, открывается черновик с пометкой источника.
    const doc = { name: 'Из файла', description: 'Проверка импорта', params: [], steps: [{ id: 's1', action: 'navigate', target: { url: 'https://files.example/start' }, value: null, secret_ref: null, safe_to_retry: true, risk: 'none' }] };
    await dlg.locator('#im-file').setInputFiles({ name: 'iz-faila.json', mimeType: 'application/json', buffer: Buffer.from(JSON.stringify(doc)) });
    await expect(text).toHaveValue(/Из файла/);
    await dlg.getByRole('button', { name: 'Импортировать' }).click();
    await expect(page).toHaveURL(/#\/procedures\/pr\d+$/);
    await expect(page.locator('#pr-root h2').first()).toHaveText('Из файла');
    await expect(page.locator('.proc-kv', { hasText: 'Источник' })).toContainText('Импортирована из файла');
    await expect(page.locator('#pr-root').getByText('Черновик', { exact: true }).first()).toBeVisible();
    expect(await stepTitles(page)).toEqual(['Открыть files.example/start']);
  });

  test('удаление: 409 при активном запуске, затем удаление без запусков', async ({ page }) => {
    await openProc(page, 'pr1', '&runs=manual');
    await page.getByRole('button', { name: 'Удалить', exact: true }).click();
    let dlg = dialog(page);
    await expect(dlg.getByRole('heading', { name: 'Удалить процедуру?' })).toBeVisible();
    await expect(dlg.getByRole('button', { name: 'Отмена' })).toBeFocused();
    await dlg.getByRole('button', { name: 'Удалить процедуру' }).click();
    await expect(dlg.getByRole('alert')).toContainText('Нельзя удалить: идёт запуск');
    await expect(page).toHaveURL(/#\/procedures\/pr1$/);
    await dlg.getByRole('button', { name: 'Отмена' }).click();
    // Процедура без запусков удаляется, список без неё.
    await openProc(page, 'pr2');
    await page.getByRole('button', { name: 'Удалить', exact: true }).click();
    dlg = dialog(page);
    await dlg.getByRole('button', { name: 'Удалить процедуру' }).click();
    await expect(page).toHaveURL(/#\/procedures$/);
    await expect(page.locator('.proc-row')).toHaveCount(2);
    await expect(page.getByText('Отклик на вакансию')).toHaveCount(0);
  });

  test('процедура не найдена', async ({ page }) => {
    await page.goto('/?mock=1#/procedures/nope');
    await expect(page.getByRole('heading', { name: 'Процедура не найдена' })).toBeVisible();
  });
});
