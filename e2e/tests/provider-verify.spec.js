import { expect, test } from '@playwright/test';

// PWA11 (docs/contracts.md §11): проверка ключа до сохранения, secret_tail, статус pending_admin у участника
// и экран администратора «Запросы на внутренние адреса».
// Мок-режим: ?mock=1 (по умолчанию админ), &role=member, &providers=private (три провайдера с внутренним адресом),
// &requests=1|stale|fail (запросы админу, первое одобрение с 409, первая загрузка не удалась).
// Триггеры проверки: ключ с «bad» даёт 422 key_rejected, «offline» unreachable, «weird» incompatible, «limit» 429.
// Адрес: «down» unreachable, «notapi» incompatible, localhost запрещён (invalid_base_url), 192.168.* уходит админу.
// Вызовы создания, правки и одобрения мок пишет в window.__providerCalls: {name, id, body}.

const isMobile = (testInfo) => testInfo.project.name === 'mobile-chromium';
const FORCE = 'Сохранить без проверки';

const row = (page, name) => page.locator('.provider-row', { hasText: name });
const submitBtn = (page) => page.locator('[data-pa-submit]:visible');
const calls = (page, name) => page.evaluate((n) => (window.__providerCalls || []).filter((c) => c.name === n), name);

async function openAdd(page, query = '') {
  await page.goto(`/?mock=1${query}#/settings/providers/new`);
  await expect(page.getByRole('heading', { name: 'Новый провайдер' })).toBeVisible();
}
async function pickKind(page, label) {
  await page.getByRole('radiogroup', { name: 'Вид провайдера' }).getByRole('radio', { name: label }).click();
}
async function fillApi(page, key, name = 'Проверка ключа') {
  await page.locator('#pa-name').fill(name);
  await page.locator('#pa-key').fill(key);
  await submitBtn(page).click();
}
async function fillEndpoint(page, url, key = 'none', name = 'Свой сервер') {
  await pickKind(page, 'Свой адрес');
  await page.locator('#pa-ep-name').fill(name);
  await page.locator('#pa-url').fill(url);
  await page.locator('#pa-ep-key').fill(key);
  await submitBtn(page).click();
}
async function openReplace(page, id = 'p-openai', button = 'Заменить ключ') {
  await page.goto(`/?mock=1#/settings/providers/${id}`);
  await page.getByRole('button', { name: button }).first().click();
  const dialog = page.getByRole('dialog', { name: button === 'Заменить ключ' ? 'Заменить ключ' : 'Адрес и ключ' });
  await expect(dialog).toBeVisible();
  return dialog;
}

// ---------------------------------------------------------------------------
// 1. Проверка ключа до сохранения: ожидание, четыре 422, 429, force
// ---------------------------------------------------------------------------
test.describe('проверка ключа: ожидание кнопки', () => {
  test('пока идёт проверка: кнопка отключена, подпись «Проверяем ключ», aria-busy, повторная отправка не уходит', async ({ page }) => {
    await openAdd(page, '&probe=slow');
    await page.locator('#pa-name').fill('Ожидание');
    await page.locator('#pa-key').fill('sk-ant-waiting-key-1234');
    const btn = submitBtn(page);
    await btn.click();
    await expect(btn).toBeDisabled();
    await expect(btn).toHaveAttribute('aria-busy', 'true');
    await expect(btn).toContainText('Проверяем ключ');
    await expect(page.getByRole('status').filter({ hasText: 'Пробный запрос' })).toBeVisible();
    await expect(page.locator('#pa-key')).toHaveJSProperty('readOnly', true);
    await page.locator('#pa-key').press('Enter'); // отправка с клавиатуры во время ожидания
    await expect(page.getByText('Провайдер отвечает')).toBeVisible();
    expect(await calls(page, 'createProvider')).toHaveLength(1);
  });

  test('после отказа кнопка снова доступна, подпись «Проверить ещё раз», без aria-busy', async ({ page }) => {
    await openAdd(page);
    await fillApi(page, 'bad-key');
    await expect(page.getByRole('alert').filter({ hasText: 'Ключ отклонён' })).toBeVisible();
    const btn = submitBtn(page);
    await expect(btn).toBeEnabled();
    await expect(btn).not.toHaveAttribute('aria-busy', 'true');
    await expect(btn).toHaveText('Проверить ещё раз');
    await expect(page.locator('#pa-key')).toHaveJSProperty('readOnly', false);
  });
});

test.describe('проверка ключа: отказы 422 и 429', () => {
  test('key_rejected: «Ключ отклонён», ошибка у поля ключа, силой не сохраняется', async ({ page }) => {
    await openAdd(page);
    await fillApi(page, 'bad-key');
    const alert = page.getByRole('alert').filter({ hasText: 'Ключ отклонён' });
    await expect(alert).toBeFocused();
    await expect(alert).toContainText('Провайдер не сохранён');
    await expect(page.locator('#pa-key')).toHaveAttribute('aria-invalid', 'true');
    await expect(page.locator('#pa-key-note')).toHaveText('Провайдер не принял этот ключ');
    await expect(alert.getByRole('button', { name: FORCE })).toHaveCount(0);
  });

  test('unreachable: «Нет связи с провайдером», объяснение про 30 секунд и «Сохранить без проверки»', async ({ page }) => {
    await openAdd(page);
    await fillApi(page, 'offline-key-123456');
    const alert = page.getByRole('alert').filter({ hasText: 'Нет связи с провайдером' });
    await expect(alert).toBeFocused();
    await expect(alert).toContainText('не дождался ответа');
    await expect(alert).toContainText('30 секунд');
    await expect(alert).toContainText('Провайдер сохранится со статусом «Не проверен»');
    await expect(alert.getByRole('button', { name: FORCE })).toBeVisible();
    await alert.getByText('Подробнее').click();
    await expect(alert).toContainText('connection failed');
  });

  test('incompatible: «Провайдер ответил не так, как ожидалось» и «Сохранить без проверки»', async ({ page }) => {
    await openAdd(page);
    await fillApi(page, 'weird-key-1234567');
    const alert = page.getByRole('alert').filter({ hasText: 'Провайдер ответил не так, как ожидалось' });
    await expect(alert).toBeVisible();
    await expect(alert).toContainText('списка моделей в нужном виде в нём нет');
    await expect(alert.getByRole('button', { name: FORCE })).toBeVisible();
  });

  test('свой адрес, incompatible: сказано про /v1, ошибка у поля адреса', async ({ page }) => {
    await openAdd(page);
    await fillEndpoint(page, 'https://notapi.example.org/v1');
    const alert = page.getByRole('alert').filter({ hasText: 'Адрес не похож на сервер моделей' });
    await expect(alert).toBeVisible();
    await expect(alert).toContainText('/v1');
    await expect(page.locator('#pa-url')).toHaveAttribute('aria-invalid', 'true');
    await expect(page.locator('#pa-url-note')).toHaveText('Этот адрес отвечает не как API моделей');
    await expect(alert.getByRole('button', { name: FORCE })).toBeVisible();
  });

  test('свой адрес, unreachable: ошибка у поля адреса, ключ не подсвечен', async ({ page }) => {
    await openAdd(page);
    await fillEndpoint(page, 'https://down.example.org/v1');
    await expect(page.getByRole('alert').filter({ hasText: 'Нет связи с провайдером' })).toBeVisible();
    await expect(page.locator('#pa-url')).toHaveAttribute('aria-invalid', 'true');
    await expect(page.locator('#pa-url-note')).toHaveText('Этот адрес не отвечает');
    await expect(page.locator('#pa-ep-key')).not.toHaveAttribute('aria-invalid', 'true');
  });

  test('invalid_base_url: «Адрес не принят», без «Сохранить без проверки» (force правила адреса не обходит)', async ({ page }) => {
    await openAdd(page);
    await fillEndpoint(page, 'https://localhost:11434/v1');
    const alert = page.getByRole('alert').filter({ hasText: 'Адрес не принят' });
    await expect(alert).toBeVisible();
    await expect(alert).toContainText('не содержать логин и пароль');
    await expect(page.locator('#pa-url')).toHaveAttribute('aria-invalid', 'true');
    await expect(alert.getByRole('button', { name: FORCE })).toHaveCount(0);
    expect(await calls(page, 'createProvider')).toHaveLength(1);
  });

  test('429 rate_limited: «Слишком много проверок подряд», без «Сохранить без проверки», повтор после правки', async ({ page }) => {
    await openAdd(page);
    await fillApi(page, 'limit-key-1234567');
    const alert = page.getByRole('alert').filter({ hasText: 'Проверка не запущена' });
    await expect(alert).toBeFocused();
    await expect(alert).toContainText('Слишком много проверок подряд, попробуйте через минуту');
    await expect(alert.getByRole('button', { name: FORCE })).toHaveCount(0);
    await expect(alert.getByText('Подробнее')).toHaveCount(0);
    await expect(page.locator('#pa-key')).not.toHaveAttribute('aria-invalid', 'true'); // ключ не виноват
    await page.locator('#pa-key').fill('sk-ant-retry-key-1234');
    await submitBtn(page).click();
    await expect(page.getByText('Провайдер отвечает')).toBeVisible();
  });

  test('отказ не оставляет провайдера в списке и не сохраняет ключ в странице', async ({ page }) => {
    await openAdd(page);
    await fillApi(page, 'bad-secret-9999');
    await expect(page.getByRole('alert').filter({ hasText: 'Ключ отклонён' })).toBeVisible();
    await page.evaluate(() => { location.hash = '#/settings/providers'; });
    await expect(page.locator('.provider-row')).toHaveCount(6);
    await expect(row(page, 'Проверка ключа')).toHaveCount(0);
  });
});

test.describe('проверка ключа: «Сохранить без проверки» (force: true)', () => {
  test('создание: повтор того же тела с force: true, статус «Не проверен», модели не запрашиваются', async ({ page }) => {
    await openAdd(page);
    await fillApi(page, 'offline-key-123456', 'Без проверки');
    const alert = page.getByRole('alert').filter({ hasText: 'Нет связи с провайдером' });
    await alert.getByRole('button', { name: FORCE }).click();
    await expect(page.getByRole('heading', { name: 'Сохранён без проверки' })).toBeVisible();
    await expect(page.locator('.state-box')).toContainText('Боты не смогут отвечать, пока проверка не пройдёт');
    const sent = await calls(page, 'createProvider');
    expect(sent).toHaveLength(2);
    expect(sent[0].body.force).toBeUndefined();
    expect(sent[1].body.force).toBe(true);
    expect(sent[1].body.name).toBe('Без проверки');
    expect(sent[1].body.kind).toBe('anthropic_api');
    await expect(page.locator('#pa-key')).toHaveCount(0); // поле с ключом убрано
    expect(await page.content()).not.toContain('offline-key-123456');
    await page.getByRole('link', { name: 'Открыть провайдера' }).first().click();
    await expect(page.locator('[data-provider-pending="unchecked"]')).toContainText('Сохранён без проверки');
    await expect(page.getByRole('heading', { name: 'Модели · включено 0 из 0' })).toBeVisible();
  });

  test('свой адрес, incompatible: force работает и там', async ({ page }) => {
    await openAdd(page);
    await fillEndpoint(page, 'https://notapi.example.org/v1', 'none', 'Сервер без модели');
    await page.getByRole('alert').getByRole('button', { name: FORCE }).click();
    await expect(page.getByRole('heading', { name: 'Сохранён без проверки' })).toBeVisible();
    const sent = await calls(page, 'createProvider');
    expect(sent[sent.length - 1].body.force).toBe(true);
    expect(sent[sent.length - 1].body.base_url).toBe('https://notapi.example.org/v1');
  });

  test('во время сохранения кнопка отключена: «Сохраняем без проверки»', async ({ page }) => {
    await openAdd(page);
    await fillApi(page, 'offline-key-123456');
    const force = page.getByRole('button', { name: FORCE });
    await force.click();
    await expect(submitBtn(page)).toContainText('Сохраняем без проверки');
    await expect(page.getByRole('heading', { name: 'Сохранён без проверки' })).toBeVisible();
    expect(await calls(page, 'createProvider')).toHaveLength(2);
  });

  test('правка поля снимает отказ вместе с кнопкой: force нельзя применить к устаревшему телу', async ({ page }) => {
    await openAdd(page);
    await fillApi(page, 'offline-key-123456');
    await expect(page.getByRole('button', { name: FORCE })).toBeVisible();
    await page.locator('#pa-key').fill('offline-key-654321');
    await expect(page.getByRole('button', { name: FORCE })).toHaveCount(0);
    await expect(page.getByRole('alert')).toHaveCount(0);
  });

  test('замена ключа: unreachable даёт «Сохранить без проверки», PATCH уходит с force: true, диалог закрывается', async ({ page }) => {
    const dialog = await openReplace(page);
    const key = dialog.getByLabel('Новый API-ключ');
    await key.fill('offline-key-1234');
    await dialog.getByRole('button', { name: 'Сохранить и проверить' }).click();
    const alert = dialog.getByRole('alert');
    await expect(alert).toContainText('Нет связи с провайдером');
    await expect(alert).toContainText('прежний ключ и адрес на месте');
    await expect(key).toHaveValue('offline-key-1234'); // введённое не теряется
    await alert.getByRole('button', { name: FORCE }).click();
    await expect(page.getByRole('dialog')).toHaveCount(0);
    const sent = await calls(page, 'patchProvider');
    expect(sent[sent.length - 1].body.force).toBe(true);
    expect(sent[sent.length - 1].id).toBe('p-openai');
    await expect(page.locator('[data-provider-pending="unchecked"]')).toContainText('Сохранён без проверки');
    await expect(page.getByText('•••• 1234').first()).toBeVisible();
    expect(await page.content()).not.toContain('offline-key-1234');
  });

  test('замена ключа: 429 и отклонённый ключ без «Сохранить без проверки»', async ({ page }) => {
    const dialog = await openReplace(page);
    const key = dialog.getByLabel('Новый API-ключ');
    await key.fill('limit-key-123456');
    await dialog.getByRole('button', { name: 'Сохранить и проверить' }).click();
    await expect(dialog.getByRole('alert')).toContainText('Слишком много проверок подряд, попробуйте через минуту');
    await expect(dialog.getByRole('button', { name: FORCE })).toHaveCount(0);
    await key.fill('bad-key-123456');
    await dialog.getByRole('button', { name: 'Сохранить и проверить' }).click();
    await expect(dialog.getByRole('alert')).toContainText('Ключ отклонён');
    await expect(dialog.getByRole('button', { name: FORCE })).toHaveCount(0);
  });

  test('смена адреса на запрещённый (localhost): «Адрес не принят» у поля, без «Сохранить без проверки», диалог открыт', async ({ page }) => {
    const dialog = await openReplace(page, 'p-ollama', 'Изменить адрес или ключ');
    await dialog.getByLabel('Адрес сервера моделей').fill('https://localhost:11434/v1');
    await dialog.getByLabel('API-ключ').fill('none');
    await dialog.getByRole('button', { name: 'Сохранить и проверить' }).click();
    await expect(dialog.getByRole('alert')).toContainText('Адрес не принят');
    await expect(dialog.getByRole('button', { name: FORCE })).toHaveCount(0);
    await expect(dialog.getByLabel('Адрес сервера моделей')).toHaveAttribute('aria-invalid', 'true');
  });
});

// ---------------------------------------------------------------------------
// 2. secret_tail: «•••• abcd» или «Ключ сохранён», поле очищается
// ---------------------------------------------------------------------------
test.describe('secret_tail', () => {
  test('в списке и на экране провайдера: четыре последних символа, точки читаются вслух как «ключ, последние символы …»', async ({ page }) => {
    await page.goto('/?mock=1#/settings/providers');
    const anthropic = row(page, 'Anthropic API');
    await expect(anthropic).toContainText('•••• x9Qz');
    await expect(anthropic.getByRole('img', { name: 'ключ, последние символы x9Qz' })).toHaveCount(1);
    await expect(row(page, 'OpenAI API')).toContainText('Ключ сохранён'); // secret_tail: null
    await expect(row(page, 'OpenAI API')).not.toContainText('••••');
    await expect(row(page, 'Claude Code')).not.toContainText('••••'); // у подписки ключа нет
    await anthropic.click();
    await expect(page.getByText('•••• x9Qz').first()).toBeVisible();
    expect(await page.content()).not.toContain('Ключ задан');
  });

  test('null вместо хвоста (короткий ключ или строка до миграции): «Ключ сохранён», без точек', async ({ page }) => {
    await openAdd(page);
    await fillApi(page, 'short-key', 'Короткий ключ'); // короче 12 символов: secret_tail = null
    await expect(page.getByText('Провайдер отвечает')).toBeVisible();
    await page.getByRole('link', { name: 'Выбрать модели' }).click();
    await expect(page.getByText('Ключ сохранён').first()).toBeVisible();
    await expect(page.locator('[data-provider]').getByText('••••')).toHaveCount(0);
    await page.evaluate(() => { location.hash = '#/settings/providers'; });
    await expect(row(page, 'Короткий ключ')).toContainText('Ключ сохранён');
  });

  test('после создания поле ключа удаляется из страницы, значение нигде не остаётся', async ({ page }) => {
    const secret = 'sk-ant-api03-ZZTOPSECRET-w0rd';
    await openAdd(page);
    await fillApi(page, secret, 'Очистка поля');
    await expect(page.getByText('Провайдер отвечает')).toBeVisible();
    await expect(page.locator('#pa-key')).toHaveCount(0);
    await expect(page.locator('input[type="password"]')).toHaveCount(0);
    expect(await page.content()).not.toContain(secret);
    expect(await page.evaluate(() => JSON.stringify([{ ...localStorage }, { ...sessionStorage }]))).not.toContain(secret);
    await page.getByRole('link', { name: 'Выбрать модели' }).click();
    await expect(page.getByText('•••• w0rd').first()).toBeVisible(); // последние 4 символа, не весь ключ
    expect(await page.content()).not.toContain(secret);
  });

  test('после замены ключа диалог закрыт, хвост обновился, ключа в разметке нет', async ({ page }) => {
    const dialog = await openReplace(page);
    await dialog.getByLabel('Новый API-ключ').fill('sk-ant-new-good-key-7788');
    await dialog.getByRole('button', { name: 'Сохранить и проверить' }).click();
    await expect(page.getByRole('dialog')).toHaveCount(0);
    await expect(page.getByText('•••• 7788').first()).toBeVisible();
    expect(await page.content()).not.toContain('sk-ant-new-good-key-7788');
    // диалог открывается заново пустым
    await page.getByRole('button', { name: 'Действия с провайдером' }).click();
    await page.getByRole('dialog').getByRole('button', { name: /Заменить ключ/ }).click();
    await expect(page.getByRole('dialog').getByLabel('Новый API-ключ')).toHaveValue('');
    await expect(page.getByRole('dialog')).toContainText('Сейчас: •••• 7788');
  });

  test('в диалоге замены показано текущее состояние ключа, показать ключ целиком нельзя', async ({ page }) => {
    const dialog = await openReplace(page);
    await expect(dialog).toContainText('Сейчас: ключ сохранён'); // у OpenAI в моке secret_tail: null
    await expect(dialog).toContainText('Показать его нельзя');
    await expect(dialog.getByLabel('Новый API-ключ')).toHaveAttribute('type', 'password');
  });
});

// ---------------------------------------------------------------------------
// 3. pending_admin у участника: статус, объяснение, состояние бота
// ---------------------------------------------------------------------------
test.describe('участник: провайдер ждёт администратора', () => {
  const MEMBER = '&role=member&providers=private';

  test('в списке: «Ждёт одобрения администратора» и «Нужно повторное одобрение» с жёлтой точкой, не ошибка', async ({ page }) => {
    await page.goto(`/?mock=1${MEMBER}#/settings/providers`);
    await expect(row(page, 'Модель в офисе')).toContainText('Ждёт одобрения администратора');
    await expect(row(page, 'NAS с моделями')).toContainText('Нужно повторное одобрение');
    await expect(row(page, 'Домашний Ollama')).toContainText('Работает');
    await expect(row(page, 'Модель в офисе').locator('.status-dot')).toHaveClass(/attention/);
    await expect(row(page, 'Модель в офисе')).toContainText('•••• n7Pw');
  });

  test('экран провайдера: объяснение, затронутые боты, запрос отправлен, кнопки «Проверить» нет', async ({ page }) => {
    await page.goto(`/?mock=1${MEMBER}#/settings/providers/p-lan`);
    const banner = page.locator('[data-provider-pending="pending"]');
    await expect(banner).toBeVisible();
    await expect(banner).toContainText('Ждёт одобрения администратора');
    await expect(banner).toContainText('Адрес находится во внутренней сети');
    await expect(banner).toContainText('Затронуты боты: Архив');
    await expect(page.locator('[data-pending-wait]')).toContainText('Запрос отправлен');
    await expect(page.getByRole('button', { name: 'Проверить', exact: true })).toHaveCount(0);
    await expect(page.getByRole('link', { name: /Открыть запросы на внутренние адреса/ })).toHaveCount(0);
    await expect(page.getByRole('heading', { name: 'Модели · включено 1 из 1' })).toBeVisible();
    // модели видны, но подписаны причиной, а не служебным именем
    await expect(page.locator('.model-row .t-footnote').first()).toContainText('Ждёт одобрения администратора');
    await expect(page.getByRole('alert')).toHaveCount(0); // ожидание не ошибка
  });

  test('повторное одобрение: адрес изменился, объяснение и статус', async ({ page }) => {
    await page.goto(`/?mock=1${MEMBER}#/settings/providers/p-lan-re`);
    const banner = page.locator('[data-provider-pending="reapproval"]');
    await expect(banner).toContainText('Адрес сервера изменился, нужно повторное одобрение');
    await expect(banner).toContainText('указывает на другие IP-адреса');
    await expect(banner).toContainText('Затронуты боты: Кодер');
  });

  test('состояние бота: на карточке модели подсказка со ссылкой на провайдера, а не зелёный бот', async ({ page }) => {
    await page.goto(`/?mock=1${MEMBER}#/bots/archive`);
    const banner = page.locator('[data-model-problem-banner]');
    await expect(banner).toContainText('Модель в офисе: ждёт одобрения администратора');
    await expect(banner).toContainText('Бот не ответит, пока провайдер не заработает');
    await expect(banner.getByRole('link', { name: 'Открыть провайдера' })).toHaveAttribute('href', '#/settings/providers/p-lan');
    await page.goto(`/?mock=1${MEMBER}#/bots/coder`);
    await expect(page.locator('[data-model-problem-banner]')).toContainText('нужно повторное одобрение');
  });

  test('список ботов: бот на провайдере в ожидании не показан как работающий', async ({ page }, testInfo) => {
    await page.goto(`/?mock=1${MEMBER}`);
    const card = page.locator('.bot-card:has([data-bot="archive"]), .desktop-bot-row[data-bot="archive"]').first();
    await expect(card).toContainText('Провайдер: ждёт одобрения администратора');
    if (isMobile(testInfo)) {
      await expect(card.locator('.status-dot')).toHaveClass(/attention/);
      await expect(card.locator('.status-dot')).not.toHaveClass(/success/);
    }
  });

  test('в мастере бота модели провайдера в ожидании видны, но недоступны, с причиной', async ({ page }, testInfo) => {
    await page.goto(`/?mock=1${MEMBER}#/bots/new?d=${encodeURIComponent('Каждый день проверяй новые вакансии SRE')}`);
    await page.getByRole('button', { name: 'Собрать' }).click();
    await expect(page.getByLabel('Имя')).toBeVisible({ timeout: 5000 });
    if (isMobile(testInfo)) await page.locator('[data-pick-open]').click();
    const waiting = page.getByRole('radiogroup', { name: 'Модель бота' }).getByRole('radio').filter({ hasText: 'qwen3' }).and(page.locator('[data-provider="p-lan-re"]'));
    await expect(waiting).toBeDisabled();
    await expect(waiting).toContainText('Нужно повторное одобрение');
  });

  test('администратор: у провайдера в ожидании ссылка на запросы, у разрешённого блок с IP и «Отозвать»', async ({ page }) => {
    await page.goto('/?mock=1&providers=private#/settings/providers/p-lan');
    await expect(page.getByRole('link', { name: 'Открыть запросы на внутренние адреса' })).toHaveAttribute('href', '#/settings/provider-requests');
    await expect(page.locator('[data-pending-wait]')).toHaveCount(0);
    await page.goto('/?mock=1&providers=private#/settings/providers/p-lan-ok');
    await expect(page.locator('[data-private-allowed]')).toContainText('Разрешён внутренний адрес: 192.168.1.20');
    await page.getByRole('button', { name: 'Отозвать' }).click();
    const dialog = page.getByRole('dialog', { name: 'Отозвать разрешение?' });
    await expect(dialog).toContainText('перейдёт в «Ждёт одобрения администратора»');
    await dialog.getByRole('button', { name: 'Отозвать' }).click();
    await expect(page.getByRole('dialog')).toHaveCount(0);
    await expect(page.locator('[data-provider-pending="pending"]')).toBeVisible();
    const sent = await calls(page, 'allowPrivate');
    expect(sent).toEqual([{ name: 'allowPrivate', id: 'p-lan-ok', body: { allow: false } }]);
  });

  test('участник не видит блок разрешённых адресов и список IP', async ({ page }) => {
    await page.goto(`/?mock=1${MEMBER}#/settings/providers/p-lan-ok`);
    await expect(page.getByText('Модели · включено')).toBeVisible();
    await expect(page.locator('[data-private-allowed]')).toHaveCount(0);
    expect(await page.content()).not.toContain('192.168.1.20');
  });
});

// ---------------------------------------------------------------------------
// 4. Экран администратора «Запросы на внутренние адреса»
// ---------------------------------------------------------------------------
const heading = (page) => page.getByRole('heading', { name: 'Запросы на внутренние адреса' }).first();
const req = (page, id) => page.locator(`[data-request="${id}"]`);

test.describe('запросы на внутренние адреса: список', () => {
  test('участник: «Раздел только для администратора», без обращения к списку', async ({ page }) => {
    await page.goto('/?mock=1&role=member&requests=1#/settings/provider-requests');
    await expect(page.getByRole('heading', { name: 'Раздел только для администратора' })).toBeVisible();
    await expect(page.getByRole('link', { name: 'К настройкам' })).toBeVisible();
    await expect(page.locator('[data-request]')).toHaveCount(0);
  });

  test('пустое состояние: «Запросов нет» с объяснением', async ({ page }) => {
    await page.goto('/?mock=1#/settings/provider-requests');
    await expect(heading(page)).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Запросов нет' })).toBeVisible();
    await expect(page.getByText('Сервер сам внутрь сети не ходит, пока вы не разрешите')).toBeVisible();
    await expect(page.locator('[data-request]')).toHaveCount(0);
  });

  test('список: имя, почта, адрес, IP, на которые указывает имя; бейдж «Повторное одобрение» и прежние IP', async ({ page }) => {
    await page.goto('/?mock=1&requests=1#/settings/provider-requests');
    await expect(page.locator('[data-request]')).toHaveCount(3);
    await expect(page.getByRole('heading', { name: 'Запросы · 3' })).toBeVisible();
    const lan = req(page, 'r-lan');
    await expect(lan).toContainText('Llama в офисе');
    await expect(lan).toContainText('alice@example.org');
    await expect(lan).toContainText('https://llm.office.lan/v1');
    await expect(lan.locator('.ip-list li')).toHaveText(['192.168.1.20']);
    await expect(lan.getByText('Повторное одобрение')).toHaveCount(0);
    const nas = req(page, 'r-nas');
    await expect(nas.locator('.badge')).toHaveText('Повторное одобрение');
    await expect(nas).toContainText('Адрес уже разрешали, но имя сервера теперь указывает на другие IP-адреса');
    await expect(nas.getByText('Было разрешено')).toBeVisible();
    await expect(nas.locator('.ip-list').nth(0)).toHaveText(/10\.0\.0\.9.*10\.0\.0\.10/);
    await expect(nas.locator('.ip-list').nth(1)).toHaveText('10.0.0.5');
    await expect(nas.getByRole('button', { name: 'Разрешить заново: NAS с моделями' })).toBeEnabled();
    await expect(nas.getByRole('button', { name: 'Отозвать разрешение: NAS с моделями' })).toBeVisible();
    // ни ключа, ни хвоста ключа, ни моделей на экране нет
    await expect(page.locator('[data-request]').first()).not.toContainText('••••');
  });

  test('имя не разрешается (resolve_error): «Разрешить» недоступна, причина объяснена и привязана через aria-describedby', async ({ page }) => {
    await page.goto('/?mock=1&requests=1#/settings/provider-requests');
    const gone = req(page, 'r-gone');
    await expect(gone).toContainText('Имя сервера сейчас не разрешается в адрес, разрешать нечего');
    const allow = gone.getByRole('button', { name: 'Разрешить: Сервер без DNS' });
    await expect(allow).toBeDisabled();
    const hint = await allow.getAttribute('aria-describedby');
    await expect(page.locator(`[id="${hint}"]`)).toContainText('разрешать нечего');
    await expect(gone.getByRole('button', { name: 'Отклонить: Сервер без DNS' })).toBeEnabled();
  });

  test('загрузка не удалась: состояние с «Повторить», решения не менялись', async ({ page }) => {
    await page.goto('/?mock=1&requests=fail#/settings/provider-requests');
    await expect(page.getByRole('heading', { name: 'Запросы не загрузились' })).toBeVisible();
    await expect(page.getByText('Решения по адресам не менялись')).toBeVisible();
    await page.getByRole('button', { name: 'Повторить' }).click();
    await expect(page.locator('[data-request]')).toHaveCount(3);
  });

});

test.describe('запросы на внутренние адреса: бейдж в настройках', () => {
  test('админ: строка «Запросы на внутренние адреса» со счётчиком и подписью для скринридера', async ({ page }) => {
    await page.goto('/?mock=1&requests=1#/settings');
    const link = page.getByRole('link', { name: /Запросы на внутренние адреса/ });
    await expect(link).toBeVisible();
    const badge = page.locator('[data-requests-badge]');
    await expect(badge).toBeVisible();
    await expect(badge).toContainText('3');
    await expect(badge.locator('.sr-only')).toContainText('запроса ждут решения');
    await expect(link).toHaveAttribute('href', '#/settings/provider-requests');
    expect((await link.boundingBox()).height).toBeGreaterThanOrEqual(44);
    await link.click();
    await expect(page).toHaveURL(/#\/settings\/provider-requests$/);
    await expect(page.locator('[data-request]')).toHaveCount(3);
  });

  test('нет запросов: строка есть, бейджа нет', async ({ page }) => {
    await page.goto('/?mock=1#/settings');
    await expect(page.getByRole('link', { name: /Запросы на внутренние адреса/ })).toBeVisible();
    await expect(page.locator('[data-requests-badge]')).toBeHidden();
  });

  test('участник: строки нет', async ({ page }) => {
    await page.goto('/?mock=1&role=member&requests=1#/settings');
    await expect(page.getByRole('heading', { name: 'Аккаунт' })).toBeVisible();
    await expect(page.getByRole('link', { name: /Запросы на внутренние адреса/ })).toHaveCount(0);
  });

  test('счётчик уменьшается после решения', async ({ page }) => {
    await page.goto('/?mock=1&requests=1#/settings/provider-requests');
    await req(page, 'r-lan').getByRole('button', { name: 'Разрешить: Llama в офисе' }).click();
    await page.getByRole('dialog').getByRole('button', { name: 'Разрешить', exact: true }).click();
    await expect(page.locator('[data-request]')).toHaveCount(2);
    await expect(page.getByRole('heading', { name: 'Запросы · 2' })).toBeVisible();
    await page.evaluate(() => { location.hash = '#/settings'; });
    await expect(page.locator('[data-requests-badge]')).toContainText('2');
  });
});

test.describe('запросы на внутренние адреса: одобрение и отказ', () => {
  test('лист подтверждения показывает адрес и IP, в PATCH уходит ровно показанное {allow, base_url, ips}', async ({ page }) => {
    await page.goto('/?mock=1&requests=1#/settings/provider-requests');
    const trigger = req(page, 'r-lan').getByRole('button', { name: 'Разрешить: Llama в офисе' });
    await trigger.click();
    const dialog = page.getByRole('dialog', { name: 'Разрешить внутренний адрес?' });
    await expect(dialog).toHaveAttribute('aria-modal', 'true');
    await expect(dialog).toContainText('Llama в офисе · alice@example.org');
    await expect(dialog.locator('[data-allow-url]')).toHaveText('https://llm.office.lan/v1');
    await expect(dialog.locator('[data-allow-ips] li')).toHaveText(['192.168.1.20']);
    await expect(dialog).toContainText('Разрешайте только те, которые знаете');
    await dialog.getByRole('button', { name: 'Разрешить', exact: true }).click();
    await expect(page.getByRole('dialog')).toHaveCount(0);
    const sent = await calls(page, 'allowPrivate');
    expect(sent).toEqual([{ name: 'allowPrivate', id: 'r-lan', body: { allow: true, base_url: 'https://llm.office.lan/v1', ips: ['192.168.1.20'] } }]);
    await expect(page.getByRole('status').filter({ hasText: 'Разрешено: Llama в офисе' })).toContainText('Провайдер проверен и работает');
    await expect(page.locator('[data-request]')).toHaveCount(2);
    await expect(req(page, 'r-lan')).toHaveCount(0);
  });

  test('во время запроса кнопки листа отключены, повторного PATCH нет', async ({ page }) => {
    await page.goto('/?mock=1&requests=1#/settings/provider-requests');
    await req(page, 'r-lan').getByRole('button', { name: 'Разрешить: Llama в офисе' }).click();
    const dialog = page.getByRole('dialog');
    const confirm = dialog.getByRole('button', { name: 'Разрешить', exact: true });
    await confirm.click();
    await expect(confirm).toBeDisabled();
    await expect(dialog.getByRole('button', { name: 'Отмена' })).toBeDisabled();
    await expect(page.getByRole('dialog')).toHaveCount(0);
    expect(await calls(page, 'allowPrivate')).toHaveLength(1);
  });

  test('повторное одобрение: лист «Разрешить заново?», уходят актуальные IP из resolved_ips', async ({ page }) => {
    await page.goto('/?mock=1&requests=1#/settings/provider-requests');
    await req(page, 'r-nas').getByRole('button', { name: 'Разрешить заново: NAS с моделями' }).click();
    const dialog = page.getByRole('dialog', { name: 'Разрешить заново?' });
    await expect(dialog.locator('[data-allow-ips] li')).toHaveText(['10.0.0.9', '10.0.0.10']);
    await dialog.getByRole('button', { name: 'Разрешить', exact: true }).click();
    await expect(page.getByRole('dialog')).toHaveCount(0);
    const sent = await calls(page, 'allowPrivate');
    expect(sent[0].body).toEqual({ allow: true, base_url: 'https://nas.lan/v1', ips: ['10.0.0.9', '10.0.0.10'] });
  });

  test('409: адрес изменился между показом и отправкой, список обновлён, новый набор можно одобрить заново', async ({ page }) => {
    await page.goto('/?mock=1&requests=stale#/settings/provider-requests');
    await req(page, 'r-lan').getByRole('button', { name: 'Разрешить: Llama в офисе' }).click();
    await page.getByRole('dialog').getByRole('button', { name: 'Разрешить', exact: true }).click();
    await expect(page.getByRole('dialog')).toHaveCount(0);
    const alert = page.getByRole('alert').filter({ hasText: 'Адрес изменился, проверьте ещё раз' });
    await expect(alert).toBeFocused();
    await expect(alert).toContainText('Список обновлён');
    await expect(page.getByRole('status').filter({ hasText: 'Разрешено' })).toHaveCount(0); // успеха нет
    await expect(req(page, 'r-lan').locator('.ip-list li')).toHaveText(['192.168.1.77']); // показан новый набор
    // повторное одобрение уже по новому набору
    await req(page, 'r-lan').getByRole('button', { name: 'Разрешить: Llama в офисе' }).click();
    await expect(page.getByRole('dialog').locator('[data-allow-ips] li')).toHaveText(['192.168.1.77']);
    await page.getByRole('dialog').getByRole('button', { name: 'Разрешить', exact: true }).click();
    await expect(page.getByRole('dialog')).toHaveCount(0);
    await expect(page.getByRole('alert')).toHaveCount(0);
    const sent = await calls(page, 'allowPrivate');
    expect(sent).toHaveLength(2);
    expect(sent[0].body.ips).toEqual(['192.168.1.20']);
    expect(sent[1].body.ips).toEqual(['192.168.1.77']);
    await expect(req(page, 'r-lan')).toHaveCount(0);
  });

  test('Escape и «Отмена» закрывают лист без запроса, фокус возвращается на «Разрешить»', async ({ page }) => {
    await page.goto('/?mock=1&requests=1#/settings/provider-requests');
    const trigger = req(page, 'r-lan').getByRole('button', { name: 'Разрешить: Llama в офисе' });
    await trigger.focus();
    await trigger.click();
    await expect(page.getByRole('dialog')).toBeVisible();
    await page.keyboard.press('Escape');
    await expect(page.getByRole('dialog')).toHaveCount(0);
    await expect(trigger).toBeFocused();
    await trigger.click();
    await page.getByRole('dialog').getByRole('button', { name: 'Отмена' }).click();
    await expect(page.getByRole('dialog')).toHaveCount(0);
    expect(await calls(page, 'allowPrivate')).toHaveLength(0);
  });

  test('отказ: лист объясняет последствия, PATCH {allow: false} без ips, запрос остаётся с пометкой', async ({ page }) => {
    await page.goto('/?mock=1&requests=1#/settings/provider-requests');
    await req(page, 'r-lan').getByRole('button', { name: 'Отклонить: Llama в офисе' }).click();
    const dialog = page.getByRole('dialog', { name: 'Отклонить запрос?' });
    await expect(dialog).toContainText('Ждёт одобрения администратора');
    await expect(dialog).toContainText('боты на его моделях не заработают');
    await dialog.getByRole('button', { name: 'Отклонить', exact: true }).click();
    await expect(page.getByRole('dialog')).toHaveCount(0);
    const sent = await calls(page, 'allowPrivate');
    expect(sent).toEqual([{ name: 'allowPrivate', id: 'r-lan', body: { allow: false } }]);
    await expect(page.getByRole('status').filter({ hasText: 'Адрес не разрешён: Llama в офисе' })).toBeVisible();
    const lan = req(page, 'r-lan');
    await expect(lan.locator('[data-rejected]')).toContainText('Вы не разрешили этот адрес');
    await expect(lan.getByRole('button', { name: 'Отклонить: Llama в офисе' })).toHaveCount(0);
    await expect(lan.getByRole('button', { name: 'Разрешить: Llama в офисе' })).toBeEnabled(); // передумать можно
  });

  test('повторный запрос: «Отозвать разрешение» снимает прежнее разрешение, PATCH {allow: false}', async ({ page }) => {
    await page.goto('/?mock=1&requests=1#/settings/provider-requests');
    await req(page, 'r-nas').getByRole('button', { name: 'Отозвать разрешение: NAS с моделями' }).click();
    const dialog = page.getByRole('dialog', { name: 'Отозвать разрешение?' });
    await expect(dialog).toContainText('Прежнее разрешение снимется');
    await dialog.getByRole('button', { name: 'Отозвать', exact: true }).click();
    await expect(page.getByRole('dialog')).toHaveCount(0);
    expect((await calls(page, 'allowPrivate'))[0].body).toEqual({ allow: false });
    await expect(req(page, 'r-nas').locator('.badge')).toHaveCount(0); // флаг и набор сняты, это уже обычный запрос
    await expect(req(page, 'r-nas').locator('[data-rejected]')).toBeVisible();
  });

});

// ---------------------------------------------------------------------------
// 5. Доступность: 393x852, 44px, фокус, aria, состояния
// ---------------------------------------------------------------------------
test.describe('доступность новых экранов', () => {
  const tooSmall = (page, selector) => page.locator(selector).evaluateAll((els) => els
    .filter((el) => el.offsetParent !== null)
    .map((el) => ({ label: (el.getAttribute('aria-label') || el.textContent).trim().slice(0, 40), h: Math.round(el.getBoundingClientRect().height) }))
    .filter((x) => x.h < 44));
  const noOverflow = (page) => page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth);

  test('экран запросов: кнопки и ссылки не меньше 44 px, без горизонтальной прокрутки', async ({ page }) => {
    await page.goto('/?mock=1&requests=1#/settings/provider-requests');
    await expect(page.locator('[data-request]')).toHaveCount(3);
    expect(await tooSmall(page, '[data-request] .btn')).toEqual([]);
    expect(await noOverflow(page)).toBe(true);
  });

  test('лист подтверждения: кнопки не меньше 44 px, фокус внутри листа, без горизонтальной прокрутки', async ({ page }) => {
    await page.goto('/?mock=1&requests=1#/settings/provider-requests');
    await req(page, 'r-nas').getByRole('button', { name: 'Разрешить заново: NAS с моделями' }).click();
    const dialog = page.getByRole('dialog');
    expect(await tooSmall(page, '.dialog .btn, .dialog .icon-btn')).toEqual([]);
    expect(await dialog.evaluate((el) => el.contains(document.activeElement))).toBe(true);
    expect(await noOverflow(page)).toBe(true);
    await expect(dialog).toHaveAttribute('aria-labelledby', 'dlg-title');
  });

  test('IP-адреса: список с подписью, у каждой кнопки доступное имя с названием провайдера', async ({ page }) => {
    await page.goto('/?mock=1&requests=1#/settings/provider-requests');
    await expect(req(page, 'r-lan').getByRole('list', { name: 'IP-адреса' })).toBeVisible();
    const names = await page.locator('[data-request] .btn').evaluateAll((els) => els.map((el) => el.getAttribute('aria-label')));
    expect(names.every((n) => n && /: /.test(n))).toBe(true);
    await expect(page.locator('#pr-note')).toHaveAttribute('role', 'status');
    await expect(page.locator('#pr-note')).toHaveAttribute('aria-live', 'polite');
  });

  test('клавиатура: Tab доходит до «Разрешить», Enter открывает лист', async ({ page }) => {
    await page.goto('/?mock=1&requests=1#/settings/provider-requests');
    const allow = req(page, 'r-lan').getByRole('button', { name: 'Разрешить: Llama в офисе' });
    await allow.focus();
    await expect(allow).toBeFocused();
    await page.keyboard.press('Enter');
    await expect(page.getByRole('dialog', { name: 'Разрешить внутренний адрес?' })).toBeVisible();
  });

  test('форма добавления: ошибки проверки в role=alert с фокусом, ожидание в role=status, кнопки не меньше 44 px', async ({ page }) => {
    await openAdd(page);
    await fillApi(page, 'offline-key-123456');
    const alert = page.getByRole('alert').filter({ hasText: 'Нет связи с провайдером' });
    await expect(alert).toBeFocused();
    expect(await tooSmall(page, '#pa-form button, #pa-form input, [data-pa-submit]')).toEqual([]);
    expect(await noOverflow(page)).toBe(true);
    await alert.getByRole('button', { name: FORCE }).focus();
    await expect(alert.getByRole('button', { name: FORCE })).toBeFocused();
  });

  test('диалог замены ключа: 44 px, ошибка с фокусом, aria-modal', async ({ page }) => {
    const dialog = await openReplace(page);
    await expect(dialog).toHaveAttribute('aria-modal', 'true');
    await dialog.getByLabel('Новый API-ключ').fill('offline-key-1234');
    await dialog.getByRole('button', { name: 'Сохранить и проверить' }).click();
    await expect(dialog.getByRole('alert')).toBeFocused();
    expect(await tooSmall(page, '.dialog .btn, .dialog input')).toEqual([]);
    expect(await noOverflow(page)).toBe(true);
  });

  test('экран провайдера в ожидании: баннер role=status, кнопка «Открыть запросы» не меньше 44 px', async ({ page }) => {
    await page.goto('/?mock=1&providers=private#/settings/providers/p-lan');
    await expect(page.locator('[data-provider-pending]')).toHaveAttribute('role', 'status');
    expect(await tooSmall(page, '[data-pending-action]')).toEqual([]);
    expect(await noOverflow(page)).toBe(true);
  });

  test('без жаргона: на новых экранах нет «base_url», «ips», «allow_private», «PATCH», «422», «DNS rebinding»', async ({ page }) => {
    const JARGON = /base_url|\bips\b|allow_private|PATCH|\b42[29]\b|rebinding|pending_admin|secret_tail|unreachable|key_rejected/i;
    for (const hash of ['#/settings/provider-requests', '#/settings/providers/p-lan', '#/settings/providers/p-lan-re', '#/settings']) {
      await page.goto(`/?mock=1&providers=private&requests=1${hash}`);
      await expect(page.locator('#app')).not.toBeEmpty();
      await page.waitForTimeout(400);
      const text = await page.locator('#app').innerText();
      expect(text, hash).not.toMatch(JARGON);
    }
    await openAdd(page);
    await fillApi(page, 'offline-key-123456');
    await expect(page.getByRole('alert')).toBeVisible();
    expect(await page.locator('#app').innerText()).not.toMatch(JARGON);
  });

  test('в текстах новых экранов нет длинного тире', async ({ page }) => {
    for (const hash of ['#/settings/provider-requests', '#/settings/providers/p-lan', '#/settings/providers/p-lan-re']) {
      await page.goto(`/?mock=1&providers=private&requests=1${hash}`);
      await page.waitForTimeout(400);
      expect(await page.locator('#app').innerText(), hash).not.toContain('—');
    }
  });
});
