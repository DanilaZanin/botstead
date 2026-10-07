import { expect as baseExpect, test } from '@playwright/test';

const expect = baseExpect;

// Провайдеры, модели, выбор модели бота, состояния ботов и терминал входа по подписке.
// Мок-режим: ?mock=1 (по умолчанию вошёл админ), &role=member, &auth=none, &providers=none|fail|slow,
// &bots=states (Архив без модели, Кодер не запустился, SRE ждёт перезапуска, Запасной без истории),
// &login=busy|forbidden|revoked|lost|timeout|start (сбои терминала); window.__loginMock.silentDrop() молча обрывает
// соединение терминала, как у свёрнутой страницы на телефоне. &login=silent|nocode|foreign|mixed с &login_timeout=<мс>:
// терминал молчит, codex без кода, ссылка на чужой хост, чужая ссылка после настоящей; срок ожидания ссылки сокращён с
// 60 с. Вывод мока повторяет фрагменты настоящих CLI (цвета, гиперссылка OSC 8 с адресом, перенесённым по строкам, код
// устройства codex); у codex вход завершает window.__loginMock.approve() или expire(). Ключи-триггеры (проверка до сохранения, §11):
// «bad» даёт 422 key_rejected, «offline» unreachable, «weird» incompatible, «limit» 429 rate_limited.
// Адреса-триггеры: «down» не отвечает, «notapi» не OpenAI-совместимый, 192.168.* и *.lan уходят админу (pending_admin),
// localhost и 127.* всегда запрещены. Подробные сценарии проверки ключа и запросов админу: provider-verify.spec.js.

const isMobile = (testInfo) => testInfo.project.name === 'mobile-chromium';

// Терминал входа держится на цепочках таймеров мока (запуск 250 мс, вывод, выход, проверка входа, отчёт о ботах): под нагрузкой
// обычные 5 с на проверку мало, поэтому в блоках терминала входа срок ожидания 15 с. Строгость проверок не меняется.
const termExpect = baseExpect.configure({ timeout: 15000 });
// &login_delay=0 снимает 2,5-секундную паузу мока перед выводом (она для глаз). Тесты, которым нужен видимый шаг 1,
// берут &login_hold=1 и сами решают, когда терминал начнёт печатать: release(page) отпускает вывод текущего сокета.
async function release(page) {
  await baseExpect.poll(() => page.evaluate(() => {
    const m = window.__loginMock;
    if (!m || !m.socket || !m.socket.releaseOutput) return false;
    m.release();
    return true;
  }), { message: 'сокет терминала входа открыт и ждёт release', timeout: 15000 }).toBe(true);
}
// Срок ожидания ссылки в моке ручной (&login_timeout=manual): ждём, пока экран его взведёт, и «истекаем» вызовом.
async function expireLinkTimer(page) {
  await baseExpect.poll(() => page.evaluate(() => typeof (window.__loginMock && window.__loginMock.linkTimeout)), { message: 'таймер ссылки взведён', timeout: 15000 }).toBe('function');
  await page.evaluate(() => window.__loginMock.linkTimeout());
}
const SECRET = 'sk-ant-TESTSECRET-4f2a91';

const list = (page) => page.locator('.provider-row');
const row = (page, name) => page.locator('.provider-row', { hasText: name });

async function openAdd(page, query = '') {
  await page.goto(`/?mock=1${query}#/settings/providers/new`);
  await expect(page.getByRole('heading', { name: 'Новый провайдер' })).toBeVisible();
}
async function pickKind(page, label) {
  await page.getByRole('radiogroup', { name: 'Вид провайдера' }).getByRole('radio', { name: label }).click();
}
async function submit(page, name = 'Проверить и сохранить') {
  await page.getByRole('button', { name }).click();
}

// Выбор модели: на телефоне лист по кнопке, на Mac блок прямо в карточке.
async function openPicker(page, testInfo) {
  if (isMobile(testInfo)) {
    await page.locator('[data-pick-open]').click();
    await expect(page.getByRole('dialog', { name: 'Модель бота' })).toBeVisible();
  }
}

test.describe('список провайдеров', () => {
  test('провайдеры со статусом: вход выполнен, работает, ключ отклонён, не отвечает, нужен вход', async ({ page }) => {
    await page.goto('/?mock=1#/settings/providers');
    await expect(list(page)).toHaveCount(6);
    await expect(row(page, 'Claude Code')).toContainText('Подписка · 3 модели');
    await expect(row(page, 'Claude Code')).toContainText('Вход выполнен');
    await expect(row(page, 'Anthropic API')).toContainText('•••• x9Qz · 2 модели');
    await expect(row(page, 'Anthropic API')).toContainText('Работает · проверен 5 мин назад');
    await expect(row(page, 'OpenAI API')).toContainText('модели выключены');
    await expect(row(page, 'OpenAI API')).toContainText('Ключ отклонён');
    await expect(row(page, 'Ollama дома')).toContainText('ollama.example.org/v1 · 2 модели');
    await expect(row(page, 'Ollama дома')).toContainText('Не отвечает');
    await expect(row(page, 'Codex')).toContainText('Подписка · моделей нет');
    await expect(row(page, 'Codex')).toContainText('Нужен вход');
  });

  test('главное действие «Добавить провайдера»: внизу на телефоне, не меньше 44 px', async ({ page }, testInfo) => {
    await page.goto('/?mock=1#/settings/providers');
    const add = page.getByRole('link', { name: 'Добавить провайдера' });
    await expect(add).toBeVisible();
    const box = await add.boundingBox();
    expect(box.height).toBeGreaterThanOrEqual(44);
    if (isMobile(testInfo)) expect(box.y + box.height).toBeGreaterThan(852 - 120);
    await expect(page.locator('.provider-row').first()).toBeVisible();
    const rows = await page.locator('.provider-row').evaluateAll((els) => els.map((el) => el.getBoundingClientRect().height));
    expect(rows.filter((h) => h < 44)).toEqual([]);
  });

  test('пустое состояние с действием', async ({ page }) => {
    await page.goto('/?mock=1&providers=none#/settings/providers');
    await expect(page.getByRole('heading', { name: 'Провайдеров нет' })).toBeVisible();
    await expect(page.getByText('Боты не смогут отвечать, пока не подключена хотя бы одна модель')).toBeVisible();
    await page.getByRole('link', { name: 'Добавить провайдера' }).click();
    await expect(page).toHaveURL(/#\/settings\/providers\/new$/);
  });

  test('загрузка и ошибка с повтором', async ({ page }) => {
    await page.goto('/?mock=1&providers=slow#/settings/providers');
    await expect(page.getByRole('status').filter({ hasText: 'Проверяю провайдеров' })).toBeVisible();
    await expect(list(page)).toHaveCount(6);

    await page.goto('/?mock=1&providers=fail#/settings/providers');
    const alert = page.getByRole('alert').filter({ hasText: 'Провайдеры не загрузились' });
    await expect(alert).toBeVisible();
    await expect(alert).toContainText('Боты продолжают работать на прежних моделях');
    await page.getByRole('button', { name: 'Повторить' }).click();
    await expect(list(page)).toHaveCount(6);
  });

  test('боты без модели: плашка со списком имён', async ({ page }) => {
    await page.goto('/?mock=1&bots=states#/settings/providers');
    await expect(page.getByRole('status').filter({ hasText: '1 бот без модели' })).toContainText('Архив');
  });

  test('Mac: справа модели выбранного провайдера, на телефоне отдельный экран', async ({ page }, testInfo) => {
    await page.goto('/?mock=1#/settings/providers');
    await row(page, 'Anthropic API').click();
    await expect(page).toHaveURL(/#\/settings\/providers\/p-anthropic$/);
    await expect(page.getByRole('heading', { name: 'Модели · включено 2 из 4' })).toBeVisible();
    if (!isMobile(testInfo)) await expect(list(page)).toHaveCount(6); // список остаётся слева
  });
});

test.describe('добавление провайдера', () => {
  test('API-ключ Anthropic: проверка, успех, ключ не остаётся на странице', async ({ page }) => {
    await openAdd(page);
    const key = page.locator('#pa-key');
    await expect(page.getByText('API-ключ', { exact: true }).first()).toBeVisible();
    await expect(key).toHaveAttribute('type', 'password');
    await expect(key).toHaveAttribute('autocomplete', 'new-password');
    await page.locator('#pa-name').fill('Мой Anthropic');
    await key.fill(SECRET);
    await submit(page);
    await expect(page.getByRole('status').filter({ hasText: 'Пробный запрос' })).toBeVisible();
    await expect(page.getByText('Провайдер отвечает')).toBeVisible();
    await expect(page.getByText('Найдено моделей: 3')).toBeVisible();
    // ключ нигде не остался: ни в разметке, ни в значениях полей, ни в памяти браузера
    expect(await page.content()).not.toContain(SECRET);
    expect(await page.locator('input').evaluateAll((els) => els.map((el) => el.value).join('|'))).not.toContain(SECRET);
    expect(await page.evaluate(() => JSON.stringify([{ ...localStorage }, { ...sessionStorage }]))).not.toContain(SECRET);
    await page.getByRole('link', { name: 'Выбрать модели' }).click();
    await expect(page).toHaveURL(/#\/settings\/providers\/p-new-1$/);
    await expect(page.getByRole('heading', { name: 'Модели · включено 3 из 3' })).toBeVisible();
    expect(await page.content()).not.toContain(SECRET);
    await expect(page.getByText('•••• 2a91').first()).toBeVisible(); // только последние 4 символа
  });

  test('сервисы OpenAI и Google: название и подсказка меняются, у Google пометка про привязку', async ({ page }) => {
    await openAdd(page);
    const vendors = page.getByRole('radiogroup', { name: 'Сервис' });
    await vendors.getByRole('radio', { name: 'OpenAI' }).click();
    await expect(page.locator('#pa-name')).toHaveValue('OpenAI API');
    await expect(page.locator('#pa-key')).toHaveAttribute('placeholder', 'sk-…');
    await vendors.getByRole('radio', { name: 'Google' }).click();
    await expect(page.locator('#pa-name')).toHaveValue('Google API');
    await expect(page.getByText('привязать к боту пока нельзя')).toBeVisible();
    await page.locator('#pa-name').fill('Мой Google');
    await page.locator('#pa-key').fill('AIza-test-key');
    await submit(page);
    await expect(page.getByText('Провайдер отвечает')).toBeVisible();
    await expect(page.getByText('Привязать Google API к боту пока нельзя')).toBeVisible();
  });

  test('отклонённый ключ: подсказка, где взять ключ, провайдер не сохранён, повторная проверка', async ({ page }) => {
    await openAdd(page);
    await page.locator('#pa-name').fill('Новый Anthropic');
    await page.locator('#pa-key').fill('bad-key');
    await submit(page);
    const alert = page.getByRole('alert').filter({ hasText: 'Ключ отклонён' });
    await expect(alert).toBeVisible();
    await expect(alert).toContainText('Провайдер не сохранён');
    await expect(alert).toContainText('console.anthropic.com');
    await alert.getByText('Подробнее').click();
    await expect(alert).toContainText('provider returned 401');
    await expect(page.locator('#pa-key')).toHaveAttribute('aria-invalid', 'true');
    // провайдер действительно не остался в списке
    await page.goto('/?mock=1#/settings/providers');
    await expect(list(page)).toHaveCount(6);
  });

  test('после отказа можно исправить ключ и проверить ещё раз', async ({ page }) => {
    await openAdd(page);
    await page.locator('#pa-name').fill('Новый Anthropic');
    await page.locator('#pa-key').fill('bad-key');
    await submit(page);
    await expect(page.getByRole('alert').filter({ hasText: 'Ключ отклонён' })).toBeVisible();
    await page.locator('#pa-key').fill('sk-ant-fixed-key');
    await expect(page.getByRole('alert')).toHaveCount(0); // правка поля снимает прежний отказ
    await submit(page, 'Проверить ещё раз');
    await expect(page.getByText('Провайдер отвечает')).toBeVisible();
  });

  test('проверка полей до отправки: пустой ключ', async ({ page }) => {
    await openAdd(page);
    await submit(page);
    await expect(page.locator('#pa-key-note')).toHaveText('Введите ключ');
    await expect(page.locator('#pa-key')).toHaveAttribute('aria-invalid', 'true');
  });

  test('свой адрес: успех', async ({ page }) => {
    await openAdd(page);
    await pickKind(page, 'Свой адрес');
    await page.locator('#pa-ep-name').fill('Мой сервер');
    await page.locator('#pa-url').fill('https://models.example.org/v1');
    await page.locator('#pa-ep-key').fill('none');
    await submit(page);
    await expect(page.getByText('Провайдер отвечает')).toBeVisible();
    await expect(page.getByText('Найдено моделей: 2')).toBeVisible();
  });

  test('свой адрес: ключ нужен всегда, объяснение для сервера без авторизации', async ({ page }) => {
    await openAdd(page);
    await pickKind(page, 'Свой адрес');
    await expect(page.getByText('если сервер ключ не проверяет')).toBeVisible();
    await page.locator('#pa-url').fill('https://models.example.org/v1');
    await submit(page);
    await expect(page.locator('#pa-ep-key-note')).toContainText('Ключ нужен всегда');
  });

  test('свой адрес: не отвечает и не OpenAI-совместимый', async ({ page }) => {
    await openAdd(page);
    await pickKind(page, 'Свой адрес');
    await page.locator('#pa-ep-name').fill('Сервер вниз');
    await page.locator('#pa-url').fill('https://down.example.org/v1');
    await page.locator('#pa-ep-key').fill('none');
    await submit(page);
    const down = page.getByRole('alert').filter({ hasText: 'Нет связи с провайдером' });
    await expect(down).toBeVisible();
    await expect(down).toContainText('Сервер botstead не дождался ответа');
    await down.getByText('Подробнее').click();
    await expect(down).toContainText('connection failed');
    await expect(page.locator('#pa-url')).toHaveAttribute('aria-invalid', 'true');

    await page.locator('#pa-url').fill('https://notapi.example.org/v1');
    await submit(page, 'Проверить ещё раз');
    await expect(page.getByRole('alert').filter({ hasText: 'Адрес не похож на сервер моделей' })).toBeVisible();
  });

  test('подписка: вид и выбор CLI, существующий провайдер ведёт в терминал входа', async ({ page }) => {
    await openAdd(page);
    await pickKind(page, 'Подписка');
    const group = page.getByRole('radiogroup', { name: 'Подписка' });
    await expect(group.getByRole('radio')).toHaveCount(3);
    await expect(group.getByRole('radio', { name: /Claude Code/ })).toHaveAttribute('aria-checked', 'true');
    await expect(page.getByText('Откроется окно входа')).toBeVisible();
    await group.getByRole('radio', { name: /Codex/ }).click();
    await submit(page, 'Открыть терминал входа');
    await expect(page).toHaveURL(/#\/settings\/providers\/p-codex\/login$/);
  });

  test('подписка без провайдеров: создаётся запись и открывается терминал входа', async ({ page }) => {
    await openAdd(page, '&providers=none');
    await pickKind(page, 'Подписка');
    await submit(page, 'Открыть терминал входа');
    await expect(page).toHaveURL(/#\/settings\/providers\/p-new-1\/login$/);
    await expect(page.getByRole('heading', { name: 'Вход: Claude Code' })).toBeVisible();
  });

  test('закрытые адреса: участнику сказано про запрос администратору, администратору про ожидание; без переключателя и без имён переменных', async ({ page }) => {
    for (const role of ['member', 'admin']) {
      await openAdd(page, role === 'member' ? '&role=member' : '');
      await pickKind(page, 'Свой адрес');
      const note = page.locator('#pa-private');
      await expect(note).toContainText('Адреса из локальной сети');
      await expect(note).toContainText(role === 'member' ? 'запрос уйдёт администратору' : 'провайдер встанет в ожидание');
      await expect(note).not.toContainText('PROVIDER_PRIVATE_ALLOW');
      await expect(page.getByRole('switch')).toHaveCount(0); // переключателя нет ни у кого

      await page.locator('#pa-ep-name').fill(`Дом ${role}`);
      await page.locator('#pa-url').fill('https://192.168.1.20:11434/v1');
      await page.locator('#pa-ep-key').fill('none');
      await submit(page);
      await expect(page.getByRole('heading', { name: 'Ждёт одобрения администратора' })).toBeVisible();
      await expect(page.getByRole('alert')).toHaveCount(0); // ожидание не ошибка
    }
  });

  test('запрещённые адреса (localhost): 422 invalid_base_url у поля адреса, без «Сохранить без проверки»', async ({ page }) => {
    await openAdd(page);
    await pickKind(page, 'Свой адрес');
    await page.locator('#pa-ep-name').fill('Локальный');
    await page.locator('#pa-url').fill('https://localhost:11434/v1');
    await page.locator('#pa-ep-key').fill('none');
    await submit(page);
    const alert = page.getByRole('alert').filter({ hasText: 'Адрес не принят' });
    await expect(alert).toBeVisible();
    await expect(alert).toContainText('Провайдер не сохранён');
    await expect(page.locator('#pa-url')).toHaveAttribute('aria-invalid', 'true');
    await expect(alert.getByRole('button', { name: 'Сохранить без проверки' })).toHaveCount(0);
  });

  test('повторное название: ошибка у поля', async ({ page }) => {
    await openAdd(page);
    await page.locator('#pa-name').fill('OpenAI API');
    await page.locator('#pa-key').fill('sk-another');
    await submit(page);
    await expect(page.locator('#pa-name-note')).toHaveText('Провайдер с таким названием уже есть');
  });

  test('поля с подписями, зоны касания не меньше 44 px', async ({ page }) => {
    await openAdd(page);
    for (const id of ['pa-key', 'pa-name']) await expect(page.locator(`label[for="${id}"]`)).toBeVisible();
    await pickKind(page, 'Свой адрес');
    for (const id of ['pa-ep-name', 'pa-url', 'pa-ep-key']) await expect(page.locator(`label[for="${id}"]`)).toBeVisible();
    const small = await page.locator('#pa-form').evaluate((form) => Array.from(form.querySelectorAll('button, input, .segmented button'))
      .filter((el) => el.offsetParent !== null)
      .map((el) => ({ label: el.id || el.textContent.trim(), h: Math.round(el.getBoundingClientRect().height) }))
      .filter((x) => x.h < 44));
    expect(small).toEqual([]);
  });
});

test.describe('модели провайдера', () => {
  test('список с переключателями, пометки, обновление списка', async ({ page }) => {
    await page.goto('/?mock=1#/settings/providers/p-anthropic');
    await expect(page.getByRole('heading', { name: 'Модели · включено 2 из 4' })).toBeVisible();
    const sonnet = page.getByRole('switch', { name: /Sonnet 5/ });
    const haiku = page.getByRole('switch', { name: /Haiku 5/ });
    await expect(sonnet).toBeChecked();
    await expect(haiku).not.toBeChecked();
    // пометки: отключена вами и пропала у провайдера (переключатель недоступен)
    await expect(page.getByText('отключена вами')).toBeVisible();
    await expect(page.getByText('провайдер больше не отдаёт эту модель')).toBeVisible();
    await expect(page.getByRole('switch', { name: /Opus 5(?!\.5)/ })).toBeDisabled();

    await haiku.check();
    await expect(page.getByRole('heading', { name: 'Модели · включено 3 из 4' })).toBeVisible();
    await expect(page.getByRole('switch', { name: /Haiku 5/ })).toBeChecked();
    await expect(page.getByText('отключена вами')).toHaveCount(0);
    await page.getByRole('switch', { name: /Sonnet 5/ }).uncheck();
    await expect(page.getByRole('heading', { name: 'Модели · включено 2 из 4' })).toBeVisible();
    await expect(page.getByText('отключена вами')).toBeVisible();

    await page.getByRole('button', { name: 'Проверить' }).click();
    await expect(page.getByRole('heading', { name: 'Модели · включено 2 из 5' })).toBeVisible();
    await expect(page.getByRole('switch', { name: /Haiku 4\.5/ })).toBeChecked(); // новая модель включилась сама
    await expect(page.getByRole('switch', { name: /Sonnet 5/ })).not.toBeChecked(); // ручное отключение сохранилось
  });

  test('пусто: провайдер-подписка без входа', async ({ page }) => {
    await page.goto('/?mock=1#/settings/providers/p-codex');
    await expect(page.getByRole('heading', { name: 'Провайдер не вернул модели' })).toBeVisible();
    await expect(page.getByText('Список появится после входа.')).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Модели · включено 0 из 0' })).toBeVisible();
    await expect(page.getByRole('link', { name: 'Войти', exact: true })).toHaveAttribute('href', '#/settings/providers/p-codex/login');
  });

  test('ошибка ключа: замена ключа, отказ и успех', async ({ page }) => {
    await page.goto('/?mock=1#/settings/providers/p-openai');
    await expect(page.getByText('Провайдер не принял ключ')).toBeVisible();
    await page.getByRole('button', { name: 'Заменить ключ' }).first().click();
    const dialog = page.getByRole('dialog', { name: 'Заменить ключ' });
    await expect(dialog).toHaveAttribute('aria-modal', 'true');
    const key = dialog.getByLabel('Новый API-ключ');
    await expect(key).toHaveAttribute('type', 'password');
    await key.fill('bad-again');
    await dialog.getByRole('button', { name: 'Сохранить и проверить' }).click();
    await expect(dialog.getByRole('alert')).toContainText('Ключ отклонён');
    await key.fill('sk-new-good-key');
    await dialog.getByRole('button', { name: 'Сохранить и проверить' }).click();
    await expect(page.getByRole('dialog')).toHaveCount(0);
    await expect(page.getByText('Работает', { exact: false }).first()).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Модели · включено 2 из 2' })).toBeVisible();
    expect(await page.content()).not.toContain('sk-new-good-key');
  });

  test('смена адреса требует повторного ввода ключа, это видно до отправки', async ({ page }) => {
    await page.goto('/?mock=1#/settings/providers/p-ollama');
    await page.getByRole('button', { name: 'Изменить адрес или ключ' }).first().click();
    const dialog = page.getByRole('dialog', { name: 'Адрес и ключ' });
    await expect(dialog.getByText('Если сменить адрес, ключ нужно ввести заново.')).toBeVisible();
    await dialog.getByLabel('Адрес сервера моделей').fill('https://ollama2.example.org/v1');
    await dialog.getByRole('button', { name: 'Сохранить и проверить' }).click();
    await expect(dialog.locator('#cr-key-note')).toHaveText('При смене адреса ключ нужно ввести заново');
    await expect(dialog).toBeVisible();
    await dialog.getByLabel('API-ключ').fill('none');
    await dialog.getByRole('button', { name: 'Сохранить и проверить' }).click();
    await expect(page.getByRole('dialog')).toHaveCount(0);
    await expect(page.getByText('ollama2.example.org/v1').first()).toBeVisible();
  });

  test('меню действий: диалог с aria-modal, Escape возвращает фокус, переименование', async ({ page }) => {
    await page.goto('/?mock=1#/settings/providers/p-anthropic');
    const menu = page.getByRole('button', { name: 'Действия с провайдером' });
    await menu.focus();
    await menu.click();
    const dialog = page.getByRole('dialog', { name: 'Anthropic API' });
    await expect(dialog).toHaveAttribute('aria-modal', 'true');
    await page.keyboard.press('Escape');
    await expect(page.getByRole('dialog')).toHaveCount(0);
    await expect(menu).toBeFocused();

    await menu.click();
    await page.getByRole('dialog').getByRole('button', { name: 'Переименовать' }).click();
    await page.getByRole('dialog').getByRole('textbox', { name: 'Название' }).fill('Anthropic рабочий');
    await page.getByRole('button', { name: 'Сохранить', exact: true }).click();
    await expect(page.getByText('Anthropic рабочий').first()).toBeVisible();
  });
});

test.describe('выбор модели бота', () => {
  const DESC = 'Каждый день проверяй новые вакансии SRE и присылай краткий список';
  async function toStep2(page, query = '') {
    await page.goto(`/?mock=1${query}#/bots/new?d=${encodeURIComponent(DESC)}`);
    await page.getByRole('button', { name: 'Собрать' }).click();
    await expect(page.getByLabel('Имя')).toBeVisible({ timeout: 5000 });
  }

  test('в мастере модель выбирается из включённых моделей провайдеров', async ({ page }, testInfo) => {
    await toStep2(page);
    await expect(page.getByText('Haiku', { exact: true })).toHaveCount(0); // зашитого списка больше нет
    await openPicker(page, testInfo);
    const radios = page.getByRole('radiogroup', { name: 'Модель бота' }).getByRole('radio');
    await expect(radios.filter({ hasText: 'Opus 5.5' })).toHaveCount(2); // Claude Code и Anthropic API
    await expect(page.getByText('Anthropic API', { exact: true })).toBeVisible();
    // модели провайдеров с ошибкой видны, но недоступны, с причиной
    const down = radios.filter({ hasText: 'llama3.3' });
    await expect(down).toBeDisabled();
    await expect(down).toContainText('Не отвечает');
    await page.locator('[role="radio"][data-provider="p-anthropic"]').filter({ hasText: 'Opus 5.5' }).click();
    if (isMobile(testInfo)) await page.getByRole('button', { name: 'Готово' }).click();
    await page.getByRole('button', { name: 'Создать', exact: true }).click();
    await expect(page).toHaveURL(/#\/threads\/t-draft-/);
    await page.getByRole('link', { name: 'Настройки бота', exact: true }).click();
    await expect(page.locator('[data-model-label]')).toHaveText('Opus 5.5');
  });

  test('моделей нет: мастер говорит об этом до описания, «Собрать» недоступно, переход к провайдерам', async ({ page }) => {
    await page.goto(`/?mock=1&providers=none#/bots/new?d=${encodeURIComponent(DESC)}`);
    const note = page.getByRole('status').filter({ hasText: 'Сначала нужна модель' });
    await expect(note).toBeVisible();
    await expect(page.getByRole('button', { name: 'Собрать' })).toBeDisabled();
    await note.getByRole('link', { name: 'Подключить модель' }).click();
    await expect(page).toHaveURL(/#\/settings\/providers$/);
  });

  test('в настройках бота: смена модели, отказ при идущей задаче, недоступные с причиной', async ({ page }, testInfo) => {
    await page.goto('/?mock=1#/bots/mac');
    await expect(page.locator('[data-model-label]')).toHaveText('Sonnet 5');
    await openPicker(page, testInfo);
    await page.locator('[role="radio"][data-provider="p-claude"]').filter({ hasText: 'Opus 5.5' }).click();
    await expect(page.locator('[data-model-label]')).toHaveText('Opus 5.5');
    // SRE сейчас работает: ядро отвечает 409, выбор не меняется
    await page.goto('/?mock=1#/bots/sre');
    await openPicker(page, testInfo);
    await page.locator('[role="radio"][data-provider="p-claude"]').filter({ hasText: 'Sonnet 5' }).click();
    await expect(page.getByRole('alert').filter({ hasText: 'Сейчас идёт задача' })).toBeVisible();
    await expect(page.locator('[data-model-label]')).toHaveText('Opus 5.5');
  });

  test('в настройках бота моделей нет: состояние и переход к провайдерам', async ({ page }) => {
    await page.goto('/?mock=1&providers=none#/bots/scout');
    await expect(page.getByRole('heading', { name: 'Нет включённых моделей' })).toBeVisible();
    await page.getByRole('link', { name: 'Открыть провайдеров' }).click();
    await expect(page).toHaveURL(/#\/settings\/providers$/);
  });

  test('клавиатура: стрелки переключают модель в радиогруппе', async ({ page }, testInfo) => {
    await page.goto('/?mock=1#/bots/mac');
    await openPicker(page, testInfo);
    const group = page.getByRole('radiogroup', { name: 'Модель бота' });
    const checked = group.locator('[aria-checked="true"]');
    await expect(checked).toContainText('Sonnet 5');
    await checked.focus();
    await page.keyboard.press('ArrowDown');
    await expect(group.locator('[aria-checked="true"]')).not.toContainText('Sonnet 5');
  });
});

test.describe('состояния ботов', () => {
  test('нет модели: карточка и тред просят выбрать модель', async ({ page }, testInfo) => {
    await page.goto('/?mock=1&bots=states');
    // карточка бота (телефон) и строка в боковой панели (Mac) называют состояние
    const card = page.locator('.bot-card:has([data-bot="archive"]), .desktop-bot-row[data-bot="archive"]').first();
    await expect(card).toContainText('Нет модели');
    await page.locator('[data-action="open-thread"][data-bot="archive"]').first().click();
    const banner = page.locator('[data-bot-state="no_model"]');
    await expect(banner).toContainText('Боту нужна модель');
    await banner.getByRole('link', { name: 'Выбрать модель' }).click();
    await expect(page).toHaveURL(/#\/bots\/archive$/);
    await expect(page.locator('[data-bot-state="no_model"]')).toBeVisible();
    // выбор модели снимает пометку, хотя статус в ядре остался прежним
    await openPicker(page, testInfo);
    await page.locator('[role="radio"][data-provider="p-claude"]').filter({ hasText: 'Haiku 4.5' }).click();
    await expect(page.locator('[data-bot-state="no_model"]')).toHaveCount(0);
  });

  test('компьютер не запустился: пересоздание с объяснением, что сохранится', async ({ page }) => {
    await page.goto('/?mock=1&bots=states');
    await page.locator('[data-action="open-thread"][data-bot="coder"]').first().click();
    const banner = page.locator('[data-bot-state="error_starting"]');
    await expect(banner).toContainText('Компьютер бота не запустился');
    await expect(banner).toContainText('Файлы бота и сохранённые входы сохранятся');
    await expect(banner).not.toContainText(/контейнер|на томе|том с (его|файлами)/i);
    const button = banner.getByRole('button', { name: 'Пересоздать компьютер' });
    const box = await button.boundingBox();
    expect(box.height).toBeGreaterThanOrEqual(44);
    await button.click();
    await expect(page.locator('[data-bot-state="error_starting"]')).toHaveCount(0);
    await expect(page.locator('#thread-body')).toBeVisible();
  });

  test('ожидает перезапуска: «перезапустится после текущей задачи» в карточке и треде', async ({ page }, testInfo) => {
    await page.goto('/?mock=1&bots=states');
    if (isMobile(testInfo)) await expect(page.locator('.bot-card:has([data-bot="sre"])')).toContainText('Перезапустится после текущей задачи');
    await page.locator('[data-action="open-thread"][data-bot="sre"]').first().click();
    await expect(page.locator('[data-bot-state="need_restart"]')).toContainText('Перезапустится после текущей задачи');
  });
});

test.describe('приветствие', () => {
  async function welcome(page, query) {
    await page.goto(`/?mock=1&auth=none${query}#/invite/valid-token`);
    await page.getByLabel('Email', { exact: true }).fill('new@example.org');
    await page.getByLabel('Пароль', { exact: true }).fill('long-enough-pass');
    await page.getByLabel('Пароль ещё раз', { exact: true }).fill('long-enough-pass');
    await page.getByRole('button', { name: 'Создать аккаунт' }).click();
    await expect(page.getByRole('heading', { name: 'Аккаунт создан' })).toBeVisible();
  }

  test('есть включённые модели: шаг про модель ведёт в провайдеры, «Создать бота» активен', async ({ page }) => {
    await welcome(page, '');
    await expect(page.getByRole('link', { name: /Модель/ }).first()).toHaveAttribute('href', '#/settings/providers');
    await expect(page.getByRole('link', { name: 'Создать бота' })).toBeEnabled();
    await expect(page.locator('.step-card[aria-disabled="true"]')).toHaveCount(0);
  });

  test('моделей нет: «Создать бота» недоступен, главное действие ведёт в провайдеры', async ({ page }) => {
    await welcome(page, '&providers=none');
    await expect(page.locator('.step-card[aria-disabled="true"]')).toContainText('Создать бота');
    await expect(page.getByRole('link', { name: 'Создать бота' })).toHaveCount(0);
    await page.getByRole('link', { name: 'Подключить модель' }).click();
    await expect(page).toHaveURL(/#\/settings\/providers$/);
  });
});

test.describe('удаление', () => {
  test('бота без истории: подтверждение объясняет, что удалится и что будет с файлами', async ({ page }) => {
    await page.goto('/?mock=1&bots=states#/bots/spare');
    await page.getByRole('button', { name: 'Удалить бота' }).click();
    const dialog = page.getByRole('dialog', { name: 'Удалить бота?' });
    await expect(dialog).toContainText('Компьютер бота удалится');
    await expect(dialog).toContainText('Файлы бота и сохранённые входы сохранятся');
    await expect(dialog).not.toContainText(/контейнер|на томе|том с (его|файлами)/i);
    await dialog.getByRole('button', { name: 'Отмена' }).click();
    await expect(page.getByRole('dialog')).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Удалить бота' })).toBeFocused();
    await page.getByRole('button', { name: 'Удалить бота' }).click();
    await page.getByRole('dialog').getByRole('button', { name: 'Удалить бота', exact: true }).click();
    await expect(page).toHaveURL(/#\/$/);
    await expect(page.locator('[data-action="open-thread"][data-bot="spare"]')).toHaveCount(0);
  });

  test('бота с историей сервер не удаляет: причины списком до подтверждения, кнопки удаления нет', async ({ page }) => {
    await page.goto('/?mock=1#/bots/archive');
    await page.getByRole('button', { name: 'Удалить бота' }).click();
    const dialog = page.getByRole('dialog', { name: 'Удалить бота?' });
    await expect(dialog).toContainText('Бот не удаляется, пока у него есть история');
    const reasons = dialog.getByRole('list', { name: 'Что мешает удалению' }).getByRole('listitem');
    await expect(reasons).toHaveText(['1 тред', '2 расписания']);
    await expect(dialog).toContainText('Архивировать бота пока нельзя');
    await expect(dialog.getByRole('button', { name: 'Удалить бота', exact: true })).toHaveCount(0);
    await dialog.getByRole('button', { name: 'Понятно' }).click();
    await expect(page.getByRole('dialog')).toHaveCount(0);
    await expect(page).toHaveURL(/#\/bots\/archive$/);
  });

  test('провайдера: подтверждение называет ботов, которые останутся без модели', async ({ page }) => {
    await page.goto('/?mock=1&bots=states#/settings/providers/p-ollama');
    await page.getByRole('button', { name: 'Удалить провайдера' }).click();
    const dialog = page.getByRole('dialog', { name: 'Удалить провайдера?' });
    // в этом режиме Архив уже без модели, поэтому у Ollama зависимых ботов нет
    await expect(dialog).toContainText('Ботов с моделью этого провайдера нет');
    await dialog.getByRole('button', { name: 'Отмена' }).click();

    await page.goto('/?mock=1#/settings/providers/p-ollama');
    await page.getByRole('button', { name: 'Удалить провайдера' }).click();
    const withBots = page.getByRole('dialog', { name: 'Удалить провайдера?' });
    await expect(withBots).toContainText('Без модели останутся 1 бот: Архив');
    await expect(withBots).toContainText('Ключ на сервере и список моделей удалятся');
    await withBots.getByRole('button', { name: 'Удалить', exact: true }).click();
    await expect(page).toHaveURL(/#\/settings\/providers$/);
    await expect(row(page, 'Ollama дома')).toHaveCount(0);
    await expect(page.getByRole('status').filter({ hasText: '1 бот без модели' })).toContainText('Архив');
  });
});

test.describe('терминал входа по подписке', () => {
  const expect = termExpect;
  // По умолчанию claude: сценарий «код с сайта» (поле «Код из браузера»). Сценарий codex «код с экрана» задаётся явно.
  // &cli_auth=out: подписки не залогинены, иначе проверка входа при открытии экрана сразу показывает «Вход уже выполнен».
  const login = (id = 'p-claude', query = '') => `/?mock=1&cli_auth=out&login_delay=0${query}#/settings/providers/${id}/login`;
  const term = (page) => page.locator('#cl-term');
  const status = (page) => page.locator('#cl-status');
  const closeLink = (page, testInfo) => (isMobile(testInfo)
    ? page.getByRole('link', { name: 'Закрыть терминал' })
    : page.getByRole('link', { name: 'Закрыть', exact: true }));

  async function sendCode(page, code) {
    await page.getByLabel('Код из браузера').fill(code);
    await page.getByRole('button', { name: 'Отправить' }).click();
  }

  // Адрес входа claude приходит гиперссылкой OSC 8 и разрезан по кадрам, а видимый текст перенесён по строкам терминала:
  // целый адрес (с state=mock-state в конце) можно получить только из OSC 8.
  const CLAUDE_LINK = /^https:\/\/claude\.com\/cai\/oauth\/authorize\?code=true&client_id=[\w-]+&response_type=code&.*&code_challenge_method=S256&state=mock-state$/;

  test('claude, код с сайта: целый адрес из OSC 8, код из браузера уходит в терминал, результат', async ({ page }, testInfo) => {
    const logs = [];
    page.on('console', (message) => logs.push(message.text()));
    await page.goto(login('p-claude'));
    await expect(page.getByRole('heading', { name: 'Вход: Claude Code' })).toBeVisible();
    await expect(status(page)).toContainText('Шаг 2 из 3. Откройте ссылку и войдите');
    await expect(term(page)).toHaveAttribute('role', 'log');
    await expect(term(page)).toHaveAttribute('aria-label', 'Терминал входа');
    // Видимый буфер короткий (на телефоне несколько строк): начало вывода из него уходит, а целую ссылку проверяет href ниже.
    // Поэтому по терминалу сверяем последнюю строку вывода, она всегда на виду.
    await expect(term(page)).toContainText('Paste code here if prompted');
    // ссылка из вывода дублируется обычной кнопкой: новая вкладка, без передачи opener
    const open = page.getByRole('link', { name: 'Открыть страницу входа' });
    await expect(open).toHaveAttribute('href', CLAUDE_LINK);
    await expect(open).toHaveAttribute('target', '_blank');
    await expect(open).toHaveAttribute('rel', /noopener/);
    if (!isMobile(testInfo)) await expect(page.getByRole('list').filter({ hasText: 'Запуск входа на сервере' }).getByRole('listitem').nth(1)).toHaveAttribute('aria-label', 'Шаг 2: идёт');
    // в этом сценарии код вводят в поле на странице, карточки кода устройства нет
    await expect(page.locator('#cl-device-card')).toHaveCount(0);
    await expect(page.getByLabel('Код из браузера')).toBeVisible();
    // поле кода: код и Enter уходят в терминал, эхо замаскировано
    await sendCode(page, 'ok');
    await expect(term(page)).toContainText('Login successful.');
    await expect(status(page)).toContainText('Вход выполнен');
    await expect(status(page)).toBeFocused(); // фокус на итоге, а не на скрытом поле кода
    await expect(page.locator('#cl-result')).toContainText('Перезапущены бот: Мак.');
    await expect(page.getByLabel('Код из браузера')).toHaveValue('');
    await expect(page.getByRole('link', { name: 'Выбрать модели' })).toHaveAttribute('href', '#/settings/providers/p-claude');
    // вывод терминала нигде не сохранён: ни в памяти браузера, ни в консоли
    expect(await page.evaluate(() => JSON.stringify([{ ...localStorage }, { ...sessionStorage }]))).not.toMatch(/oauth|Login successful/);
    expect(logs.join('\n')).not.toMatch(/oauth|Login successful|Opening browser/);
  });

  test('agy, код с сайта: ссылка accounts.google.com из OSC 8 целиком, поле «Код из браузера»', async ({ page }) => {
    await page.goto(login('p-agy'));
    await expect(page.getByRole('heading', { name: 'Вход: Antigravity' })).toBeVisible();
    await expect(status(page)).toContainText('Шаг 2 из 3. Откройте ссылку и войдите');
    await expect(page.getByRole('link', { name: 'Открыть страницу входа' })).toHaveAttribute('href', /^https:\/\/accounts\.google\.com\/o\/oauth2\/auth\?client_id=mock-agy\.apps\.googleusercontent\.com&.*&state=mock-state$/);
    await expect(page.locator('#cl-device-card')).toHaveCount(0);
    await sendCode(page, 'ok');
    await expect(status(page)).toContainText('Вход выполнен');
  });

  test('codex, код с экрана: ссылка и код устройства отдельно, поля кода нет, вход завершается сам', async ({ page, context }, testInfo) => {
    await context.grantPermissions(['clipboard-read', 'clipboard-write']);
    const logs = [];
    page.on('console', (message) => logs.push(message.text()));
    await page.goto(login('p-codex'));
    await expect(page.getByRole('heading', { name: 'Вход: Codex' })).toBeVisible();
    await expect(status(page)).toContainText('Шаг 2 из 3. Откройте ссылку и войдите');
    await expect(status(page)).toContainText('Шаг 3: ввести на ней код с экрана.');
    await expect(term(page)).toContainText('Never share this code'); // последние строки вывода всегда на виду, начало на телефоне прокручено
    const open = page.getByRole('link', { name: 'Открыть страницу входа' });
    await expect(open).toHaveAttribute('href', 'https://auth.openai.com/codex/device');
    await expect(open).toHaveAttribute('rel', /noopener/);
    // код пришёл двумя кадрами («ABCD-12» и «345»): показан только целый
    await expect(page.locator('#cl-device-card')).toBeVisible();
    await expect(page.locator('#cl-device-code')).toHaveText('ABCD-12345');
    // поля «Код из браузера» в этом сценарии нет: код вводят на сайте
    await expect(page.locator('#cl-form')).toBeHidden();
    await expect(page.getByLabel('Код из браузера')).toBeHidden();
    // переход по ссылке (в новую вкладку не уходим) открывает шаг 3
    await open.evaluate((el) => el.addEventListener('click', (e) => e.preventDefault()));
    await open.click();
    await expect(status(page)).toContainText('Шаг 3 из 3. Введите код на странице входа');
    await expect(status(page)).toContainText('Вход завершится сам');
    if (!isMobile(testInfo)) await expect(page.getByRole('list').filter({ hasText: 'Запуск входа на сервере' }).getByRole('listitem').nth(2)).toHaveAttribute('aria-label', 'Шаг 3: идёт');
    // копирование кода
    await page.getByRole('button', { name: 'Скопировать код' }).click();
    await expect(page.locator('#cl-device-note')).toContainText('Код скопирован.');
    expect(await page.evaluate(() => navigator.clipboard.readText())).toBe('ABCD-12345');
    // сайт принял код: CLI завершился сам, проверка подписки и результат
    await page.evaluate(() => window.__loginMock.approve());
    await expect(term(page)).toContainText('Successfully logged in');
    await expect(status(page)).toContainText('Вход выполнен');
    await expect(status(page)).toBeFocused();
    await expect(page.locator('#cl-device-card')).toBeHidden(); // код отработал, на экране его не оставляем
    await expect(page.locator('#cl-result')).toContainText('Найдено моделей: 1.');
    await expect(page.locator('#cl-result')).toContainText('Ботов с этим провайдером пока нет.');
    await expect(page.getByRole('link', { name: 'Выбрать модели' })).toHaveAttribute('href', '#/settings/providers/p-codex');
    // вывод терминала и код устройства нигде не сохранены: ни в памяти браузера, ни в консоли
    expect(await page.evaluate(() => JSON.stringify([{ ...localStorage }, { ...sessionStorage }]))).not.toMatch(/ABCD-12345|device|Successfully/);
    expect(logs.join('\n')).not.toMatch(/ABCD-12345|one-time code|Successfully logged in/);
    // статус провайдера обновился
    await page.goto('/?mock=1#/settings/providers');
    await expect(row(page, 'Codex')).toContainText('Вход выполнен');
    await expect(row(page, 'Codex')).toContainText('Подписка · 1 модель');
  });

  test('codex: код просрочен или не принят, главное действие «Получить новый код», старый код скрыт', async ({ page }) => {
    await page.goto(login('p-codex'));
    await expect(page.locator('#cl-device-code')).toHaveText('ABCD-12345');
    await page.evaluate(() => window.__loginMock.expire());
    await expect(term(page)).toContainText('device auth timed out');
    const alert = page.getByRole('alert').filter({ hasText: 'Код не принят или просрочен' });
    await expect(alert).toBeVisible();
    await expect(page.locator('#cl-device-card')).toBeHidden();
    await expect(page.locator('#cl-link-card')).toBeHidden();
    await expect(page.locator('#cl-actions .btn-primary')).toHaveCount(1);
    await page.getByRole('button', { name: 'Получить новый код' }).click();
    await expect(status(page)).toContainText('Шаг 2 из 3. Откройте ссылку и войдите');
    await expect(page.locator('#cl-device-code')).toHaveText('ABCD-12345');
  });

  // Срок ожидания ссылки 60 с. В моке он ручной (&login_timeout=manual): тест сам «истекает» его через expireLinkTimer,
  // поэтому шаг 1 до этого момента держится сколько нужно, а не 4 с на часах.
  test('терминал молчит: за отведённое время ссылки нет, сессия закрывается, предлагается начать заново', async ({ page }) => {
    await page.goto(login('p-claude', '&login=silent&login_timeout=manual'));
    await expect(status(page)).toContainText('Шаг 1 из 3. Жду ссылку для входа');
    await expireLinkTimer(page);
    const alert = page.getByRole('alert').filter({ hasText: 'Ссылка для входа не появилась' });
    await expect(alert).toBeVisible();
    await expect(alert).toContainText('Сессия на сервере закрыта');
    await expect(page.locator('#cl-link-card')).toBeHidden();
    await expect(page.locator('#cl-form')).toBeHidden();
    await expect(page.locator('#cl-actions .btn-primary')).toHaveCount(1);
    await expect(page.locator('#cl-actions .btn-primary')).toContainText('Начать заново');
    // сессия на сервере закрыта кадром close
    expect(await page.evaluate(() => window.__loginMock.frames.some((f) => f.t === 'close'))).toBe(true);
    // заново: сокет открывается снова и снова ждёт ссылку
    await page.getByRole('button', { name: 'Начать заново' }).click();
    await expect(status(page)).toContainText('Шаг 1 из 3');
  });

  test('codex: ссылка есть, а кода нет: тот же срок, сообщение про код', async ({ page }) => {
    await page.goto(login('p-codex', '&login=nocode&login_timeout=manual'));
    await expect(page.getByRole('link', { name: 'Открыть страницу входа' })).toHaveAttribute('href', 'https://auth.openai.com/codex/device');
    await expect(status(page)).toContainText('Шаг 1 из 3. Жду ссылку и код для входа'); // без кода шаг 2 не наступает
    await expect(page.locator('#cl-device-card')).toBeHidden();
    await expireLinkTimer(page);
    await expect(page.getByRole('alert').filter({ hasText: 'Код для входа не появился' })).toBeVisible();
    await expect(page.locator('#cl-link-card')).toBeHidden();
  });

  test('ссылка на чужой хост не становится кнопкой: ни в OSC 8, ни простым текстом', async ({ page }) => {
    await page.goto(login('p-claude', '&login=foreign&login_timeout=manual'));
    await expect(term(page)).toContainText('evil.example');
    await expect(page.locator('#cl-link-card')).toBeHidden();
    await expect(page.getByRole('link', { name: 'Открыть страницу входа' })).toBeHidden();
    await expect(status(page)).toContainText('Шаг 1 из 3. Жду ссылку для входа');
    await expireLinkTimer(page);
    await expect(page.getByRole('alert').filter({ hasText: 'Ссылка для входа не появилась' })).toBeVisible();
  });

  test('codex: чужая ссылка и настоящий код, шага 2 нет', async ({ page }) => {
    await page.goto(login('p-codex', '&login=foreign&login_timeout=manual'));
    await expect(term(page)).toContainText('evil.example');
    await expect(page.locator('#cl-link-card')).toBeHidden();
    await expect(status(page)).toContainText('Шаг 1 из 3');
    await expireLinkTimer(page);
    await expect(page.getByRole('alert').filter({ hasText: 'Ссылка для входа не появилась' })).toBeVisible();
  });

  test('чужая ссылка после настоящей пропускается: кнопка остаётся на хосте провайдера', async ({ page }) => {
    await page.goto(login('p-claude', '&login=mixed'));
    await expect(status(page)).toContainText('Шаг 2 из 3. Откройте ссылку и войдите');
    await expect(term(page)).toContainText('evil.example');
    await expect(page.getByRole('link', { name: 'Открыть страницу входа' })).toHaveAttribute('href', CLAUDE_LINK);
  });

  test('разбор вывода в браузере: OSC 8, код устройства, список хостов', async ({ page }) => {
    await page.goto('/?mock=1#/');
    const result = await page.evaluate(async () => {
      const m = await import('/terminal.js');
      const url = 'https://claude.com/cai/oauth/authorize?code=true&client_id=x&state=abc';
      const link = `\x1b]8;;${url}\x1b\\https://claude.com/cai/oauth/auth\r\norize?code=true&client_id=x&state=abc\x1b]8;;\x1b\\\r\n`;
      const codex = '2. Enter this one-time code \x1b[90m(expires in 15 minutes)\x1b[0m\r\n   \x1b[94mABCD-12345\x1b[0m\r\n';
      return {
        osc: m.findLoginUrl(link) === url,
        partial: m.findLoginUrl(link.slice(0, 40)) === '',
        plain: m.findLoginUrl('   \x1b[94mhttps://auth.openai.com/codex/device\x1b[0m\r\n'),
        code: m.findDeviceCode(codex),
        codePartial: m.findDeviceCode(codex.slice(0, codex.indexOf('ABCD-12') + 8)),
        hosts: [m.isLoginUrlAllowed('claude', url), m.isLoginUrlAllowed('claude', 'https://evil.example/'), m.isLoginUrlAllowed('claude', 'https://claude.ai.evil.example/'), m.isLoginUrlAllowed('codex', url)],
        filtered: m.findLoginUrl('https://claude.ai/ok \r\nhttps://evil.example/x \r\n', (u) => m.isLoginUrlAllowed('claude', u)),
      };
    });
    expect(result).toEqual({
      osc: true, partial: true, plain: 'https://auth.openai.com/codex/device', code: 'ABCD-12345', codePartial: '',
      hosts: [true, false, false, false], filtered: 'https://claude.ai/ok',
    });
  });

  test('английский интерфейс: оба сценария переведены, шаги и карточка кода устройства', async ({ page }) => {
    await page.addInitScript(() => { try { localStorage.setItem('bothub.lang', 'en'); } catch { /* приватный режим */ } });
    await page.goto(login('p-claude'));
    await expect(status(page)).toContainText('Step 2 of 3. Open the link and sign in');
    await expect(page.getByLabel('Code from the browser')).toBeVisible();
    await expect(page.getByRole('link', { name: 'Open sign-in page' })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Sign-in: Claude Code' })).toBeVisible();
    await page.goto(login('p-codex'));
    await expect(status(page)).toContainText('Step 2 of 3. Open the link and sign in');
    await expect(page.locator('#cl-device-card')).toContainText('Code for the sign-in page');
    await expect(page.locator('#cl-device-card')).toContainText('Enter this code on the sign-in page');
    await expect(page.getByRole('button', { name: 'Copy code' })).toBeVisible();
    await expect(page.locator('#cl-device-code')).toHaveText('ABCD-12345'); // сам код не переводится
    expect(await page.locator('#app').innerText()).not.toMatch(/Введите|Скопировать|Откройте ссылку/);
  });

  test('после входа видно, какие боты перезапущены и какие перезапустятся позже', async ({ page }) => {
    await page.goto(login('p-claude'));
    await expect(status(page)).toContainText('Шаг 2 из 3');
    await sendCode(page, 'ok');
    await expect(status(page)).toContainText('Вход выполнен');
    await expect(page.locator('#cl-result')).toContainText('Перезапущены бот: Мак.');
    await expect(page.locator('#cl-result')).toContainText('Перезапустятся после текущей задачи: SRE.');
  });

  test('неверный код: шапка «Код не подошёл», «Получить новый код», устаревшие ссылка и поле скрыты', async ({ page }) => {
    await page.goto(login());
    await expect(status(page)).toContainText('Шаг 2 из 3');
    await sendCode(page, 'bad');
    await expect(term(page)).toContainText('OAuth error: invalid_grant');
    const alert = page.getByRole('alert').filter({ hasText: 'Код не подошёл' });
    await expect(alert).toBeVisible();
    await expect(status(page)).not.toContainText('не пройден');
    await expect(page.locator('#cl-link-card')).toBeHidden();
    await expect(page.locator('#cl-form')).toBeHidden();
    await expect(page.locator('#cl-actions .btn-primary')).toHaveCount(1);
    await page.getByRole('button', { name: 'Получить новый код' }).click();
    await expect(status(page)).toContainText('Шаг 2 из 3. Откройте ссылку и войдите');
    await expect(page.locator('#cl-link-card')).toBeVisible();
    await expect(page.getByLabel('Код из браузера')).toHaveValue('');
  });

  test('сессия уже открыта в другом месте (4409)', async ({ page }) => {
    await page.goto(`/?mock=1&login=busy&login_delay=0#/settings/providers/p-codex/login`);
    await expect(page.getByRole('alert').filter({ hasText: 'Вход уже открыт в другом месте' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Повторить' })).toBeVisible();
    await expect(page.getByRole('alert')).not.toContainText('потеряна');
    await expect(page.locator('#cl-link-card')).toBeHidden();
    await expect(page.locator('#cl-form')).toBeHidden();
  });

  test('нет прав (4404) и сессия отозвана (4401)', async ({ page }) => {
    await page.goto(`/?mock=1&login=forbidden&login_delay=0#/settings/providers/p-codex/login`);
    await expect(page.getByRole('alert').filter({ hasText: 'Нет доступа к этому входу' })).toBeVisible();
    await page.goto(`/?mock=1&login=revoked&login_delay=0#/settings/providers/p-codex/login`);
    await expect(page.getByRole('alert').filter({ hasText: 'Сессия botstead закрыта' })).toBeVisible();
  });

  test('связь потеряна: сессия на сервере закрыта, можно подключиться снова', async ({ page }) => {
    // hold: обрыв наступает по release(), а после «Подключиться снова» новый сокет молчит, и шаг 1 виден без гонки с выводом
    await page.goto(`/?mock=1&login=lost&login_delay=0&login_hold=1#/settings/providers/p-codex/login`);
    await expect(status(page)).toContainText('Шаг 1 из 3');
    await release(page);
    const alert = page.getByRole('alert').filter({ hasText: 'Связь с терминалом потеряна' });
    await expect(alert).toBeVisible();
    await expect(alert).toContainText('вход нужно начать заново');
    await expect(term(page)).toHaveClass(/is-muted/);
    await page.getByRole('button', { name: 'Подключиться снова' }).click();
    await expect(status(page)).toContainText('Шаг 1 из 3');
  });

  test('тайм-аут сессии (1001) и сбой запуска (1011)', async ({ page }) => {
    await page.goto(`/?mock=1&login=timeout&login_delay=0#/settings/providers/p-codex/login`);
    await expect(page.getByRole('alert').filter({ hasText: 'Сессия закрыта по времени' })).toContainText('30 минут');
    await page.goto(`/?mock=1&login=start&login_delay=0#/settings/providers/p-codex/login`);
    await expect(page.getByRole('alert').filter({ hasText: 'Терминал не запустился' })).toBeVisible();
  });

  test('подключение: сначала шаг 1, терминал подписан и не ловит фокус вкладкой', async ({ page }) => {
    await page.goto(login('p-claude', '&login_hold=1')); // вывода нет, пока тест не отпустит: шаг 1 не гонка с моком
    await expect(status(page)).toContainText('Шаг 1 из 3');
    await expect(term(page)).toHaveAttribute('aria-label', 'Терминал входа');
    await expect(page.getByLabel('Ввод в терминал входа')).toHaveCount(1);
    await release(page);
    await expect(status(page)).toContainText('Шаг 2 из 3');
  });

  test('resize уходит кадром по контракту: cols 20–300, rows 5–100', async ({ page }, testInfo) => {
    await page.goto(login());
    await expect(status(page)).toContainText('Шаг 2 из 3');
    const frames = await page.evaluate(() => window.__loginMock.frames);
    const resize = frames.find((f) => f.t === 'resize');
    expect(resize).toBeTruthy();
    expect(Number.isInteger(resize.cols) && Number.isInteger(resize.rows)).toBe(true);
    expect(resize.cols).toBeGreaterThanOrEqual(20);
    expect(resize.cols).toBeLessThanOrEqual(300);
    expect(resize.rows).toBeGreaterThanOrEqual(5);
    expect(resize.rows).toBeLessThanOrEqual(100);
    if (isMobile(testInfo)) expect(resize.cols).toBeLessThanOrEqual(60); // узкая колонка телефона
  });

  test('ряд клавиш над клавиатурой телефона: Esc, Tab, Ctrl залипающий, стрелки, Вставить', async ({ page }, testInfo) => {
    await page.goto(login());
    await expect(status(page)).toContainText('Шаг 2 из 3');
    const keys = page.getByRole('toolbar', { name: 'Клавиши терминала' });
    if (!isMobile(testInfo)) {
      await expect(keys).toBeHidden(); // на Mac есть настоящая клавиатура
      return;
    }
    await expect(keys).toBeVisible();
    const sizes = await keys.getByRole('button').evaluateAll((els) => els.map((el) => ({ h: el.getBoundingClientRect().height, w: el.getBoundingClientRect().width })));
    expect(sizes.length).toBe(8);
    expect(sizes.filter((s) => s.h < 44 || s.w < 44)).toEqual([]);
    await keys.getByRole('button', { name: 'Esc' }).click();
    await expect(term(page)).toContainText('<Esc>');
    await keys.getByRole('button', { name: 'Tab' }).click();
    await expect(term(page)).toContainText('<Tab>');
    await keys.getByRole('button', { name: 'Стрелка вверх' }).click();
    await expect(term(page)).toContainText('<Up>');
    await keys.getByRole('button', { name: 'Стрелка вниз' }).click();
    await expect(term(page)).toContainText('<Down>');
    // Ctrl залипает до следующей клавиши: Ctrl, затем c с клавиатуры даёт Ctrl+C
    const ctrl = keys.getByRole('button', { name: 'Ctrl' });
    await ctrl.click();
    await expect(ctrl).toHaveAttribute('aria-pressed', 'true');
    await term(page).click();
    await page.keyboard.type('c');
    await expect(term(page)).toContainText('<C-c>');
    await expect(ctrl).toHaveAttribute('aria-pressed', 'false');
    await expect(keys.getByRole('button', { name: 'Вставить' })).toBeVisible();
  });

  test('Вставить отправляет текст из буфера в терминал', async ({ page, context }, testInfo) => {
    test.skip(!isMobile(testInfo), 'ряд клавиш только на телефоне');
    await context.grantPermissions(['clipboard-read', 'clipboard-write']);
    await page.goto(login());
    await expect(status(page)).toContainText('Шаг 2 из 3');
    await page.evaluate(() => navigator.clipboard.writeText('abc'));
    await page.getByRole('toolbar', { name: 'Клавиши терминала' }).getByRole('button', { name: 'Вставить' }).click();
    await expect(term(page)).toContainText('***');
  });

  test('фокус не застревает в терминале: Shift+Esc переводит на «Закрыть», Tab из шапки доступен', async ({ page }, testInfo) => {
    await page.goto(login());
    await expect(status(page)).toContainText('Шаг 2 из 3');
    // подсказка про Shift+Esc только на компьютере: на телефоне такой клавиши нет
    if (isMobile(testInfo)) await expect(page.getByText('Shift+Esc')).toHaveCount(0);
    else await expect(page.getByText('Выйти из терминала с клавиатуры: Shift+Esc.')).toBeVisible();
    await term(page).click();
    await expect(page.getByLabel('Ввод в терминал входа')).toBeFocused();
    await page.keyboard.press('Shift+Escape');
    await expect(closeLink(page, testInfo)).toBeFocused();
    await expect(term(page)).not.toContainText('<Esc>'); // Shift+Esc в терминал не уходит
    await page.keyboard.press('Enter');
    await expect(page).toHaveURL(/#\/settings\/providers$/);
    // при выходе терминал закрывает сессию кадром close
    await expect.poll(() => page.evaluate(() => window.__loginMock.frames.some((f) => f.t === 'close'))).toBe(true);
  });

  test('кнопка «Закрыть» достижима с клавиатуры и ведёт к провайдерам', async ({ page }, testInfo) => {
    await page.goto(login());
    await expect(status(page)).toContainText('Шаг 2 из 3');
    const close = closeLink(page, testInfo);
    await close.focus();
    await expect(close).toBeFocused();
    const box = await close.boundingBox();
    expect(box.height).toBeGreaterThanOrEqual(44);
    await page.keyboard.press('Enter');
    await expect(page).toHaveURL(/#\/settings\/providers$/);
  });

  test('провайдер не подписка или удалён: понятное состояние без терминала', async ({ page }) => {
    await page.goto('/?mock=1#/settings/providers/p-anthropic/login');
    await expect(page.getByRole('heading', { name: 'Нет подписочного провайдера' })).toBeVisible();
    await page.goto('/?mock=1#/settings/providers/p-missing/login');
    await expect(page.getByRole('heading', { name: 'Нет подписочного провайдера' })).toBeVisible();
  });
});

test.describe('доступность', () => {
  test('ошибки живут в role=alert, прогресс в role=status', async ({ page }) => {
    await openAdd(page);
    await page.locator('#pa-name').fill('Проверка a11y');
    await page.locator('#pa-key').fill('bad-key');
    await submit(page);
    await expect(page.getByRole('status').filter({ hasText: 'Пробный запрос' })).toBeVisible();
    await expect(page.getByRole('alert').filter({ hasText: 'Ключ отклонён' })).toBeFocused();
  });

  test('диалог выбора модели: aria-modal, фокус возвращается на кнопку', async ({ page }, testInfo) => {
    test.skip(!isMobile(testInfo), 'лист только на телефоне');
    await page.goto('/?mock=1#/bots/mac');
    const trigger = page.locator('[data-pick-open]');
    await trigger.focus();
    await trigger.click();
    await expect(page.getByRole('dialog', { name: 'Модель бота' })).toHaveAttribute('aria-modal', 'true');
    await page.keyboard.press('Escape');
    await expect(page.getByRole('dialog')).toHaveCount(0);
    await expect(trigger).toBeFocused();
  });

  test('зоны касания: строки провайдеров, модели и варианты выбора не меньше 44 px', async ({ page }) => {
    await page.goto('/?mock=1#/settings/providers/p-anthropic');
    await expect(page.getByRole('switch').first()).toBeVisible();
    const small = await page.locator('.model-row .switch-row, .provider-row, .btn').evaluateAll((els) => els
      .filter((el) => el.offsetParent !== null)
      .map((el) => ({ label: el.textContent.trim().slice(0, 30), h: Math.round(el.getBoundingClientRect().height) }))
      .filter((x) => x.h < 44));
    expect(small).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// Правки по UX-оценке: замена ключа, закрытый адрес, выбор способа, обрыв и ошибки терминала
// ---------------------------------------------------------------------------
test.describe('замена ключа: предупреждение и честный отказ', () => {
  async function openReplace(page) {
    await page.goto('/?mock=1#/settings/providers/p-openai');
    await page.getByRole('button', { name: 'Заменить ключ' }).first().click();
    return page.getByRole('dialog', { name: 'Заменить ключ' });
  }

  test('до отправки диалог сообщает: прежний ключ остаётся, пока новый не пройдёт проверку', async ({ page }) => {
    const dialog = await openReplace(page);
    await expect(dialog.getByText('Прежний ключ остаётся, пока новый не пройдёт проверку')).toBeVisible();
    await expect(dialog).toContainText('Сервер сначала пробует новый ключ у провайдера');
    await expect(dialog.getByRole('alert')).toHaveCount(0); // сообщение не ошибка
  });

  test('отклонённый новый ключ: сказано, что прежний ключ на месте, введённое не теряется, диалог закрывается кнопкой', async ({ page }) => {
    const dialog = await openReplace(page);
    const key = dialog.getByLabel('Новый API-ключ');
    await key.fill('bad-new-key');
    await dialog.getByRole('button', { name: 'Сохранить и проверить' }).click();
    const alert = dialog.getByRole('alert');
    await expect(alert).toContainText('Ключ отклонён');
    await expect(alert).toContainText('прежний ключ и адрес на месте');
    await expect(alert).toBeFocused();
    await expect(alert.getByRole('button', { name: 'Сохранить без проверки' })).toHaveCount(0); // отказ ключа силой не обходится
    await expect(key).toHaveValue('bad-new-key');
    await key.fill('bad-again');
    await expect(dialog.getByRole('alert')).toHaveCount(0); // правка снимает прежний отказ
    await dialog.getByRole('button', { name: 'Сохранить и проверить' }).click();
    await expect(dialog.getByRole('alert')).toContainText('Ключ отклонён');
    await dialog.getByRole('button', { name: 'Отмена' }).click();
    await expect(page.getByRole('dialog')).toHaveCount(0);
    await expect(page.getByText('Ключ отклонён').first()).toBeVisible(); // провайдер по-прежнему с отклонённым ключом
    expect(await page.content()).not.toContain('bad-again');
  });

  test('модели провайдера с ошибкой подписаны причиной, а не служебным именем модели', async ({ page }) => {
    await page.goto('/?mock=1#/settings/providers/p-openai');
    const rows = page.locator('.model-row');
    await expect(rows).toHaveCount(2);
    for (const model of await rows.all()) await expect(model.locator('.t-footnote')).toContainText('Ключ отклонён');
    await page.goto('/?mock=1#/settings/providers/p-ollama');
    for (const model of await page.locator('.model-row').all()) await expect(model.locator('.t-footnote')).toContainText('Не отвечает');
  });
});

test.describe('закрытый адрес: заявка администратору вместо отказа', () => {
  async function tryPrivate(page, role) {
    await openAdd(page, role === 'member' ? '&role=member' : '');
    await pickKind(page, 'Свой адрес');
    await page.locator('#pa-ep-name').fill('Квартира');
    await page.locator('#pa-url').fill('https://192.168.1.5:11434/v1');
    await page.locator('#pa-ep-key').fill('none');
    await submit(page);
    await expect(page.getByRole('heading', { name: 'Ждёт одобрения администратора' })).toBeVisible();
  }

  test('участник: ответ простыми словами, провайдер сохранён и ждёт решения, без имён переменных и кнопки просьбы', async ({ page }) => {
    await tryPrivate(page, 'member');
    const result = page.locator('.state-box');
    await expect(result).toContainText('Адрес находится во внутренней сети');
    await expect(result).toContainText('Запрос отправлен');
    await expect(result).not.toContainText('PROVIDER_PRIVATE_ALLOW');
    await expect(page.getByRole('button', { name: 'Скопировать просьбу администратору' })).toHaveCount(0);
    await expect(page.getByRole('switch')).toHaveCount(0);
    await page.evaluate(() => { location.hash = '#/settings/providers'; }); // без перезагрузки: мок хранит данные в памяти страницы
    await expect(row(page, 'Квартира')).toContainText('Ждёт одобрения администратора');
  });

  test('при смене адреса у существующего провайдера: прежний ключ на месте, провайдер ждёт администратора', async ({ page }) => {
    await page.goto('/?mock=1&role=member#/settings/providers/p-ollama');
    await page.getByRole('button', { name: 'Изменить адрес или ключ' }).first().click();
    const dialog = page.getByRole('dialog', { name: 'Адрес и ключ' });
    await dialog.getByLabel('Адрес сервера моделей').fill('https://192.168.1.5:11434/v1');
    await dialog.getByLabel('API-ключ').fill('none');
    await dialog.getByRole('button', { name: 'Сохранить и проверить' }).click();
    await expect(page.getByRole('dialog')).toHaveCount(0);
    await expect(page.locator('[data-provider-pending="pending"]')).toContainText('Ждёт одобрения администратора');
  });
});

test.describe('способ подключения: две строки у каждого и сравнение', () => {
  test('у каждого способа: что нужно и как считается оплата, зона касания не меньше 44 px', async ({ page }) => {
    await openAdd(page);
    const group = page.getByRole('radiogroup', { name: 'Вид провайдера' });
    const cases = [
      ['API-ключ', 'Нужен ключ из личного кабинета', 'Оплата по токенам'],
      ['Свой адрес', 'Нужны адрес сервера моделей', 'Оплата зависит от сервера'],
      ['Подписка', 'Нужен аккаунт Claude', 'расход идёт в лимит тарифа'],
    ];
    for (const [name, need, pay] of cases) {
      const radio = group.getByRole('radio', { name });
      await expect(radio).toContainText(need);
      await expect(radio).toContainText(pay);
      expect((await radio.boundingBox()).height).toBeGreaterThanOrEqual(44);
    }
  });

  test('«Какой выбрать» открывает сравнение, в нём вход по подписке и безопасность', async ({ page }) => {
    await openAdd(page);
    const link = page.getByRole('button', { name: 'Какой выбрать' });
    await link.focus();
    await link.click();
    const dialog = page.getByRole('dialog', { name: 'Какой способ выбрать' });
    await expect(dialog.getByRole('region')).toHaveCount(3);
    await expect(dialog).toContainText('Как считается оплата');
    await expect(dialog).toContainText('Как устроен вход по подписке');
    await expect(dialog).toContainText('Пароль от аккаунта в botstead не вводится');
    await expect(dialog).toContainText('нигде не сохраняется');
    await page.keyboard.press('Escape');
    await expect(page.getByRole('dialog')).toHaveCount(0);
    await expect(link).toBeFocused();
  });
});

test.describe('словарь: без жаргона в экранах провайдеров', () => {
  test('нет «контейнер», «том», «Base URL», «OpenAI-совместимый», «CLI», «Вендор», «терминал на сервере»', async ({ page }) => {
    const JARGON = /контейнер|на томе|том с (его|файлами)|том бота|Base URL|OpenAI-совместим|\bCLI\b|Вендор|терминал[а-я]* на сервере|mcp__/i;
    const hashes = ['#/settings/providers', '#/settings/providers/new', '#/settings/providers/p-openai', '#/settings/providers/p-codex', '#/settings/providers/p-codex/login', '#/bots/new', '#/bots/mac'];
    for (const hash of hashes) {
      await page.goto(`/?mock=1&x=${encodeURIComponent(hash)}${hash}`);
      await expect.poll(async () => (await page.locator('#app').innerText()).length, { message: hash }).toBeGreaterThan(30);
      await page.waitForTimeout(400);
      if (hash === '#/settings/providers/new') {
        for (const kind of ['Свой адрес', 'Подписка']) { await pickKind(page, kind); expect(await page.locator('#app').innerText(), `${hash} ${kind}`).not.toMatch(JARGON); }
      }
      expect(await page.locator('#app').innerText(), hash).not.toMatch(JARGON);
    }
  });

  test('шаг 2 мастера: правила бота названы по-человечески, без имён инструментов', async ({ page }) => {
    await page.goto(`/?mock=1#/bots/new?d=${encodeURIComponent('Каждый день проверяй новые вакансии SRE и присылай краткий список')}`);
    await page.getByRole('button', { name: 'Собрать' }).click();
    await expect(page.getByLabel('Имя')).toBeVisible({ timeout: 5000 });
    const text = await page.locator('#app').innerText();
    expect(text).not.toMatch(/mcp__|контейнер/i);
    await expect(page.locator('.chip').first()).toContainText('Команды на компьютере бота');
  });
});

test.describe('терминал входа: ошибки, обрыв, клавиатура, телефон', () => {
  const expect = termExpect;
  // claude: сценарий «код с сайта», поле «Код из браузера» есть (сценарий codex проверяется выше)
  const login = (query = '') => `/?mock=1&cli_auth=out&login_delay=0${query}#/settings/providers/p-claude/login`;
  const status = (page) => page.locator('#cl-status');
  const term = (page) => page.locator('#cl-term');

  test('в состояниях ошибки ссылка и поле кода скрыты, главное действие одно', async ({ page }) => {
    const cases = [
      ['lost', 'Связь с терминалом потеряна', 'Подключиться снова'],
      ['timeout', 'Сессия закрыта по времени', 'Начать заново'],
      ['start', 'Терминал не запустился', 'Начать заново'],
      ['busy', 'Вход уже открыт в другом месте', 'Повторить'],
      ['forbidden', 'Нет доступа к этому входу', 'Закрыть'],
    ];
    for (const [mode, title, primary] of cases) {
      await page.goto(login(`&login=${mode}`));
      await expect(page.getByRole('alert').filter({ hasText: title })).toBeVisible();
      await expect(page.locator('#cl-link-card'), mode).toBeHidden();
      await expect(page.locator('#cl-form'), mode).toBeHidden();
      await expect(page.getByRole('link', { name: 'Открыть страницу входа' }), mode).toBeHidden();
      const main = page.locator('#cl-actions .btn-primary');
      await expect(main, mode).toHaveCount(1);
      await expect(main, mode).toContainText(primary);
    }
  });

  test('возврат из браузера: «Связь прервалась, пока приложение было свёрнуто», код в поле сохраняется', async ({ page }) => {
    await page.goto(login('&login_hold=1'));
    await release(page);
    await expect(status(page)).toContainText('Шаг 2 из 3');
    await page.getByLabel('Код из браузера').fill('abc-123');
    // страница уснула: сокет мёртв без события close, при возврате приходит visibilitychange
    await page.evaluate(() => { window.__loginMock.silentDrop(); document.dispatchEvent(new Event('visibilitychange')); });
    const alert = page.getByRole('alert').filter({ hasText: 'Связь прервалась, пока приложение было свёрнуто' });
    await expect(alert).toBeVisible();
    await expect(alert).toContainText('Код в поле сохранён');
    await expect(page.locator('#cl-form')).toBeHidden();
    await expect(page.locator('#cl-link-card')).toBeHidden();
    await page.getByRole('button', { name: 'Подключиться снова' }).click();
    await expect(status(page)).toContainText('Шаг 1 из 3'); // новый сокет молчит до release
    await release(page);
    await expect(status(page)).toContainText('Шаг 2 из 3. Откройте ссылку и войдите');
    await expect(page.getByLabel('Код из браузера')).toHaveValue('abc-123');
  });

  test('живое соединение при возврате не трогаем', async ({ page }) => {
    await page.goto(login());
    await expect(status(page)).toContainText('Шаг 2 из 3');
    await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
    await expect(status(page)).toContainText('Шаг 2 из 3');
    await expect(page.getByRole('alert')).toHaveCount(0);
  });

  test('под терминалом строка о том, что содержимое не сохраняется', async ({ page }) => {
    await page.goto(login());
    await expect(page.locator('#cl-save-note')).toContainText('нигде не сохраняется');
    await expect(page.locator('#cl-save-note')).toContainText('доступны только его ботам');
  });

  test('компьютер: фокус на «Открыть страницу входа», Tab не заходит в терминал, Esc уходит в команду', async ({ page }, testInfo) => {
    test.skip(isMobile(testInfo), 'клавиатурные сценарии для компьютера');
    await page.goto(login());
    await expect(page.getByRole('link', { name: 'Открыть страницу входа' })).toBeFocused();
    const seen = [];
    for (let i = 0; i < 10; i += 1) {
      await page.keyboard.press('Tab');
      seen.push(await page.evaluate(() => (document.activeElement && (document.activeElement.getAttribute('aria-label') || document.activeElement.id || document.activeElement.tagName)) || ''));
    }
    expect(seen).not.toContain('Ввод в терминал входа');
    await term(page).click();
    await expect(page.getByLabel('Ввод в терминал входа')).toBeFocused();
    await page.keyboard.press('Escape');
    await expect(term(page)).toContainText('<Esc>'); // Esc ушёл в команду, а не закрыл экран
    await expect(page).toHaveURL(/\/login$/);
  });

  test('компьютер: «К содержимому» первая по Tab и минует боковую панель', async ({ page }, testInfo) => {
    test.skip(isMobile(testInfo), 'ссылка только в десктопной раскладке');
    // На экране терминала фокус сразу ставится на главное действие, поэтому порядок Tab с начала страницы
    // проверяется на списке провайдеров: там автофокуса нет.
    await page.goto('/?mock=1#/settings/providers');
    await expect(page.locator('.desktop-main')).toBeVisible();
    // пока экран догружается, приложение не принимает ввод (inert): ждём окончания отрисовки
    await page.waitForFunction(() => !document.getElementById('app').inert);
    await page.keyboard.press('Tab');
    const link = page.getByRole('link', { name: 'К содержимому' });
    await expect(link).toBeFocused();
    await page.keyboard.press('Enter');
    await expect(page.locator('#content')).toBeFocused();
  });

  test('телефон: без подсказки про Shift+Esc, экран 393 px без обрезки, высота по видимой области', async ({ page }, testInfo) => {
    test.skip(!isMobile(testInfo), 'только телефон');
    await page.goto(login());
    await expect(status(page)).toContainText('Шаг 2 из 3');
    await expect(page.locator('#cl-kbd-hint')).toHaveCount(0);
    await expect(page.getByText('Shift+Esc')).toHaveCount(0);
    const layout = await page.evaluate(() => {
      const width = document.documentElement.clientWidth;
      const outside = [];
      for (const el of document.querySelectorAll('.app-header *, .cl-banner *, .cl-below *')) {
        const r = el.getBoundingClientRect();
        if (r.width && (r.left < -0.5 || r.right > width + 0.5)) outside.push(`${el.tagName}.${el.className}`);
      }
      return { outside, scrollWidth: document.documentElement.scrollWidth, width };
    });
    expect(layout.outside).toEqual([]);
    expect(layout.scrollWidth).toBeLessThanOrEqual(layout.width);
    // высота экрана следует visualViewport: при сжатии окна (клавиатура) ряд клавиш остаётся на виду
    await expect.poll(() => page.locator('.cl-screen').evaluate((el) => el.style.getPropertyValue('--cl-vh'))).toMatch(/^\d+px$/);
    await page.setViewportSize({ width: 393, height: 520 });
    await expect.poll(() => page.evaluate(() => Math.round(window.visualViewport.height))).toBeLessThanOrEqual(520);
    const keys = await page.locator('.term-keys').boundingBox();
    expect(keys.y + keys.height).toBeLessThanOrEqual(520.5);
    const screen = await page.locator('.cl-screen').boundingBox();
    expect(screen.y + screen.height).toBeLessThanOrEqual(520.5);
  });
});


test.describe('проверка входа при открытии экрана', () => {
  // Без &cli_auth=out: Claude Code и Antigravity в моке залогинены, Codex нет.
  const open = (id, query = '') => `/?mock=1${query}#/settings/providers/${id}/login`;
  const status = (page) => page.locator('#cl-status');
  const checks = (page) => page.evaluate(() => (window.__providerCalls || []).filter((c) => c.name === 'checkProvider'));
  const loginFrames = (page) => page.evaluate(() => (window.__loginMock ? window.__loginMock.frames.length : 0));

  test('вход выполнен: спиннер «Проверяю вход», затем «Вход уже выполнен», модели и «Готово»; терминал не открывался', async ({ page }) => {
    await page.goto(open('p-claude'));
    await expect(page.getByRole('heading', { name: 'Вход: Claude Code' })).toBeVisible();
    await expect(status(page)).toContainText('Проверяю вход');
    await expect(page.locator('#cl-term-wrap')).toBeHidden();
    await expect(status(page)).toContainText('Вход уже выполнен');
    await expect(status(page)).toBeFocused();
    await expect(page.locator('#cl-term-wrap')).toBeHidden();
    await expect(page.locator('#cl-idle')).toBeVisible();
    await expect(page.locator('#cl-result')).toContainText('Найдено моделей: 3.');
    await expect(page.locator('#cl-result .cl-models')).toContainText('claude-opus-5-5');
    await expect(page.getByRole('link', { name: 'Закрыть', exact: true })).toHaveAttribute('href', '#/settings/providers/p-claude');
    await expect(page.getByRole('button', { name: 'Войти заново' })).toBeVisible();
    // форм кода и ссылки входа нет, соединение терминала не открывалось
    await expect(page.locator('#cl-form')).toBeHidden();
    await expect(page.locator('#cl-link-card')).toBeHidden();
    expect(await loginFrames(page)).toBe(0);
    // проверка одна, без force: статус провайдера в списке был ok
    expect(await checks(page)).toEqual([{ name: 'checkProvider', id: 'p-claude', body: null }]);
  });

  test('вход не выполнен: после проверки открывается терминал, проверка с force=1, т.к. статус не ok', async ({ page }) => {
    await page.goto(open('p-codex'));
    await expect(status(page)).toContainText('Проверяю вход');
    await expect(status(page)).toContainText('Шаг 1 из 3', { timeout: 8000 });
    await expect(page.locator('#cl-term-wrap')).toBeVisible();
    await expect(page.locator('#cl-idle')).toBeHidden();
    await expect(page.getByRole('button', { name: 'Войти заново' })).toHaveCount(0);
    await expect(page.locator('#cl-device-code')).toHaveText(/^[A-Z0-9]{4}-[A-Z0-9]{5}$/, { timeout: 8000 });
    expect(await checks(page)).toEqual([{ name: 'checkProvider', id: 'p-codex', body: { force: true } }]);
  });

  test('проверка не ответила (500): терминал открывается как раньше', async ({ page }) => {
    await page.goto(open('p-claude', '&check=fail'));
    await expect(status(page)).toContainText('Шаг 2 из 3. Откройте ссылку и войдите', { timeout: 8000 });
    await expect(page.locator('#cl-term-wrap')).toBeVisible();
    await expect(page.getByRole('alert')).toHaveCount(0);
  });

  test('«Войти заново»: терминал без новой проверки, после кода exit(0) идёт «Проверяю вход» и «Вход выполнен»', async ({ page }) => {
    await page.goto(open('p-claude'));
    await expect(status(page)).toContainText('Вход уже выполнен');
    await page.getByRole('button', { name: 'Войти заново' }).click();
    await expect(page.locator('#cl-term-wrap')).toBeVisible();
    await expect(page.locator('#cl-idle')).toBeHidden();
    await expect(status(page)).toContainText('Шаг 2 из 3. Откройте ссылку и войдите', { timeout: 8000 });
    await expect(page.getByRole('button', { name: 'Войти заново' })).toHaveCount(0);
    await expect(page.locator('#cl-result')).toBeEmpty();
    await page.getByLabel('Код из браузера').fill('ok');
    await page.getByRole('button', { name: 'Отправить' }).click();
    await expect(status(page)).toContainText('Проверяю вход');
    await expect(status(page)).toContainText('Вход выполнен');
    await expect(status(page)).not.toContainText('уже');
    await expect(page.getByRole('link', { name: 'Выбрать модели' })).toHaveAttribute('href', '#/settings/providers/p-claude');
    // «Войти заново» повторных проверок при открытии не делает: одна проверка экрана
    expect((await checks(page)).length).toBe(1);
  });

  test('«Войти заново» с неверным кодом: обычная ошибка входа, а не «Вход уже выполнен»', async ({ page }) => {
    await page.goto(open('p-agy'));
    await expect(status(page)).toContainText('Вход уже выполнен');
    await page.getByRole('button', { name: 'Войти заново' }).click();
    await expect(status(page)).toContainText('Шаг 2 из 3', { timeout: 8000 });
    await page.getByLabel('Код из браузера').fill('bad');
    await page.getByRole('button', { name: 'Отправить' }).click();
    await expect(page.getByRole('alert').filter({ hasText: 'Код не подошёл' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Получить новый код' })).toBeVisible();
  });

  test('уход с экрана во время проверки: терминал не открывается', async ({ page }) => {
    await page.goto(open('p-codex'));
    await expect(status(page)).toContainText('Проверяю вход');
    await page.goto('/?mock=1#/settings/providers');
    await expect(page.getByRole('heading', { name: 'Провайдеры' }).first()).toBeVisible();
    await page.waitForTimeout(1500);
    expect(await loginFrames(page)).toBe(0);
  });
});

// Провайдер с сотнями моделей (&providers=many: OpenRouter, 60 включённых моделей acme/…, globex/…, initech/…, umbrella/…).
test.describe('много моделей у провайдера', () => {
  const MANY = '/?mock=1&providers=many#/settings/providers/p-many';
  const rows = (page) => page.locator('#pd-models .model-row');
  const heading = (page, enabled, total = 60) => page.getByRole('heading', { name: `Модели · включено ${enabled} из ${total}` });
  const searchbox = (page) => page.getByRole('searchbox', { name: 'Поиск модели' });

  test('поиск по подстроке без учёта регистра, счётчик заголовка не зависит от поиска', async ({ page }) => {
    await page.goto(MANY);
    await expect(heading(page, 60)).toBeVisible();
    await expect(rows(page)).toHaveCount(60);
    await searchbox(page).fill('model-07');
    await expect(rows(page)).toHaveCount(1);
    await expect(page.locator('#pd-filter-note')).toContainText('Найдено 1 из 60');
    await searchbox(page).fill('ACME/');
    await expect(rows(page)).toHaveCount(15);
    await expect(heading(page, 60)).toBeVisible();
    await searchbox(page).fill('нет такой');
    await expect(page.getByText('Ничего не найдено')).toBeVisible();
    await searchbox(page).fill('');
    await expect(rows(page)).toHaveCount(60);
    await expect(page.locator('#pd-filter-note')).toBeHidden();
  });

  test('«Выключить все» на найденных: до 50 без подтверждения, остальные модели не тронуты', async ({ page }) => {
    await page.goto(MANY);
    await searchbox(page).fill('acme/');
    await page.getByRole('button', { name: 'Выключить все' }).click();
    await expect(page.getByRole('dialog')).toHaveCount(0);
    await expect(heading(page, 45)).toBeVisible();
    await expect(page.locator('#pd-bulk')).toContainText('Выключено моделей: 15');
    await expect(rows(page)).toHaveCount(15);
    expect(await page.locator('#pd-models input[data-model]:checked').count()).toBe(0);
    await searchbox(page).fill('');
    await expect(rows(page)).toHaveCount(60);
    expect(await page.locator('#pd-models input[data-model]:checked').count()).toBe(45);
    await expect(rows(page).filter({ hasText: 'отключена вами' })).toHaveCount(15);
  });

  test('больше 50 найденных: подтверждение, отмена ничего не меняет', async ({ page }) => {
    await page.goto(MANY);
    await page.getByRole('button', { name: 'Выключить все' }).click();
    const dialog = page.getByRole('dialog', { name: 'Выключить все найденные модели?' });
    await expect(dialog).toBeVisible();
    await expect(dialog).toContainText('Найдено моделей: 60');
    await dialog.getByRole('button', { name: 'Отмена' }).click();
    await expect(dialog).toHaveCount(0);
    await expect(heading(page, 60)).toBeVisible();
    await page.getByRole('button', { name: 'Выключить все' }).click();
    await page.getByRole('dialog').getByRole('button', { name: 'Выключить', exact: true }).click();
    await expect(heading(page, 0)).toBeVisible();
    await expect(page.locator('#pd-bulk')).toContainText('Выключено моделей: 60');
    // и обратно: «Включить все» возвращает отключённые вручную
    await page.getByRole('button', { name: 'Включить все' }).click();
    await page.getByRole('dialog').getByRole('button', { name: 'Включить', exact: true }).click();
    await expect(heading(page, 60)).toBeVisible();
    await expect(page.locator('#pd-bulk')).toContainText('Включено моделей: 60');
  });

  test('после проверки провайдера с 60 моделями: подсказка выключить лишние; у малого списка её нет', async ({ page }) => {
    await openAdd(page);
    await pickKind(page, 'Свой адрес');
    await page.locator('#pa-ep-name').fill('OpenRouter 2');
    await page.locator('#pa-url').fill('https://many.example.org/api/v1');
    await page.locator('#pa-ep-key').fill('sk-or-test-key-123');
    await submit(page);
    await expect(page.getByText('Найдено моделей: 60')).toBeVisible();
    const hint = page.locator('[data-many-models]');
    await expect(hint).toContainText('Включены все 60');
    await expect(hint).toContainText('поиск и кнопка «Выключить все»');
  });

  test('после проверки провайдера с малым числом моделей подсказки нет', async ({ page }) => {
    await openAdd(page);
    await page.locator('#pa-name').fill('Мой Anthropic');
    await page.locator('#pa-key').fill(SECRET);
    await submit(page);
    await expect(page.getByText('Найдено моделей: 3')).toBeVisible();
    await expect(page.locator('[data-many-models]')).toHaveCount(0);
  });

  test('выбор модели бота: поле поиска и первые 12 совпадений, выбранная модель всегда в списке', async ({ page }, testInfo) => {
    await page.goto('/?mock=1&providers=many#/bots/archive');
    await expect(page.locator('[data-model-label]')).toHaveText(/llama3/);
    await openPicker(page, testInfo);
    const search = page.locator('[data-pick-search]');
    await expect(search).toBeVisible();
    const group = page.locator('.model-group').filter({ hasText: 'OpenRouter' });
    await expect(group.getByRole('radio')).toHaveCount(12);
    await expect(group).toContainText('Показаны первые 12 из 60');
    // выбранная llama3.3 (Ollama дома) остаётся в списке при любом запросе
    await search.fill('model-33');
    await expect(group.getByRole('radio')).toHaveCount(1);
    await expect(group).not.toContainText('Показаны первые');
    await expect(page.locator('.model-option[aria-checked="true"]')).toContainText('llama3.3');
    await search.fill('нет такой модели');
    await expect(page.locator('.model-option[aria-checked="true"]')).toHaveCount(1);
    await search.fill('model-33');
    await group.getByRole('radio').click();
    await expect(page.locator('.model-option[aria-checked="true"]')).toContainText('model-33');
    await expect(search).toHaveValue('model-33'); // поиск остаётся после выбора
    if (isMobile(testInfo)) await page.getByRole('button', { name: 'Готово' }).click();
    await expect(page.locator('[data-model-label]')).toHaveText(/model-33/);
  });

  test('у провайдеров с малым числом моделей поля поиска в выборе нет', async ({ page }, testInfo) => {
    await page.goto('/?mock=1#/bots/archive');
    await openPicker(page, testInfo);
    await expect(page.getByRole('radiogroup', { name: 'Модель бота' })).toBeVisible();
    await expect(page.locator('[data-pick-search]')).toHaveCount(0);
  });
});

test.describe('ошибка сервера при сохранении провайдера', () => {
  test('500: «Ошибка сервера при сохранении» и подсказка про лог ядра, а не «Сервер не отвечает»', async ({ page }) => {
    await openAdd(page, '&provider_save=500');
    await page.locator('#pa-name').fill('Мой Anthropic');
    await page.locator('#pa-key').fill(SECRET);
    await submit(page);
    const alert = page.getByRole('alert').filter({ hasText: 'Ошибка сервера при сохранении' });
    await expect(alert).toBeVisible();
    await expect(alert).toContainText('Подробности в логе ядра (docker compose logs core)');
    await expect(page.getByText('Сервер не отвечает')).toHaveCount(0);
    await expect(page.getByText('Попробуйте ещё раз через минуту')).toHaveCount(0);
  });
});
