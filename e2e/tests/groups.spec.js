import { expect, test } from '@playwright/test';

// Обсуждения ботов (группа): список, создание, тред с репликами по очереди, остановка, 409, английский, телефон.
// Мок-режим: ?mock=1; &groupdelay=<мс> задержка реплики (по умолчанию 700), &groups=none (групп нет), &groups=busy (отправка даёт 409).

const isMobile = (testInfo) => testInfo.project.name === 'mobile-chromium';
const FAST = '/?mock=1&groupdelay=150';

async function createGroup(page, { title, bots, mode, rounds }) {
  await page.goto(`${FAST}#/groups/new`);
  await expect(page.getByRole('heading', { name: 'Новое обсуждение' })).toBeVisible();
  const submit = page.getByRole('button', { name: 'Создать обсуждение' });
  await expect(submit).toBeDisabled();
  await page.getByLabel('Название').fill(title);
  await page.getByRole('checkbox', { name: new RegExp(bots[0]) }).click();
  await expect(submit).toBeDisabled(); // одного бота мало
  for (const bot of bots.slice(1)) await page.getByRole('checkbox', { name: new RegExp(bot) }).click();
  if (mode) await page.getByRole('radio', { name: new RegExp(mode) }).click();
  if (rounds) await page.getByLabel('Раундов').fill(String(rounds));
  return submit;
}

test.describe('список обсуждений', () => {
  test('показывает группу с участниками, режимом и статусом; пустой список предлагает создать', async ({ page }) => {
    await page.goto(`${FAST}#/groups`);
    const row = page.locator('.group-row').first();
    await expect(row).toContainText('Выбор стека для бота');
    await expect(row).toContainText('Скаут, Кодер');
    await expect(row).toContainText('Спор');
    await expect(row).toContainText('Ещё не запускалось');
    await page.goto(`${FAST}&groups=none#/groups`);
    await expect(page.getByText('Обсуждений пока нет')).toBeVisible();
    await page.getByRole('link', { name: 'Новое обсуждение' }).click();
    await expect(page).toHaveURL(/#\/groups\/new$/);
  });

  test('вход в раздел с главного экрана: вкладка на телефоне, пункт боковой панели на Mac', async ({ page }, testInfo) => {
    await page.goto('/?mock=1');
    const link = isMobile(testInfo) ? page.locator('nav.tabbar').getByRole('link', { name: 'Обсуждения' }) : page.locator('.desktop-sidebar').getByRole('link', { name: 'Обсуждения' });
    await expect(link).toBeVisible();
    await link.click();
    await expect(page).toHaveURL(/#\/groups$/);
    await expect(page.getByRole('heading', { name: 'Обсуждения' })).toBeVisible();
  });
});

test.describe('создание и обсуждение', () => {
  test('форма: 2 бота, режим, раунды; после создания открывается пустой тред', async ({ page }) => {
    const submit = await createGroup(page, { title: 'Облако или свой сервер', bots: ['Скаут', 'Кодер'], mode: 'Спор', rounds: 2 });
    await expect(page.locator('.group-bot-order').filter({ hasText: /^[12]$/ })).toHaveCount(2);
    await expect(submit).toBeEnabled();
    await submit.click();
    await expect(page).toHaveURL(/#\/groups\/g-\d+$/);
    await expect(page.getByRole('heading', { name: 'Облако или свой сервер' })).toBeVisible();
    await expect(page.locator('#group-input')).toBeEnabled();
  });

  test('режим с модератором показывает выбор модератора, режимы описаны', async ({ page }) => {
    await createGroup(page, { title: 'Модерация', bots: ['Скаут', 'Мак'] });
    await expect(page.locator('#gr-moderator-field')).toBeHidden();
    await expect(page.getByRole('radio', { name: /По кругу/ })).toContainText('по разу в раунде');
    await expect(page.getByRole('radio', { name: /Спор/ })).toContainText('согласны или передали слово');
    await page.getByRole('radio', { name: /С модератором/ }).click();
    await expect(page.locator('#gr-moderator')).toBeVisible();
    await expect(page.locator('#gr-moderator option')).toHaveText(['Скаут', 'Мак']);
  });

  test('спор: реплики по очереди с именами, разделители раундов, статус, согласие и конец', async ({ page }) => {
    await page.goto('/?mock=1&groupdelay=400#/groups/g-1');
    await expect(page.getByRole('heading', { name: 'Выбор стека для бота' })).toBeVisible();
    const input = page.locator('#group-input');
    await expect(input).toBeEnabled();
    await input.fill('Какой стек взять для нового бота?');
    await page.getByRole('button', { name: 'Отправить' }).click();
    await expect(page.locator('.msg-user')).toHaveText('Какой стек взять для нового бота?');
    // пока идёт: статус, кнопка «Остановить», поле заблокировано
    await expect(page.locator('#group-status')).toBeVisible();
    await expect(page.locator('#group-status')).toContainText(/Идёт раунд \d из 3 · отвечает (Скаут|Кодер)/);
    await expect(page.getByRole('button', { name: 'Остановить' })).toBeVisible();
    await expect(input).toBeDisabled();
    await expect(page.locator('#group-send')).toBeDisabled();
    await expect(page.locator('.msg-group')).toHaveCount(4, { timeout: 15000 });
    await expect(page.locator('#thread-body .system-pill[data-group-event="done"]')).toHaveText(/Обсуждение завершено/, { timeout: 10000 });
    // порядок: раунд 1, Скаут, Кодер, раунд 2, Скаут, Кодер; три раунда не нужны: оба согласились
    const order = await page.locator('#thread-body > .msg-user, #thread-body > .msg-group .msg-group-name, #thread-body > .group-divider, #thread-body > .system-pill').evaluateAll((els) => els.map((el) => el.textContent.trim().replace(/\s+/g, ' ')));
    expect(order).toEqual(['Какой стек взять для нового бота?', 'Раунд 1 из 3', 'Скаут', 'Кодер', 'Раунд 2 из 3', 'Скаут', 'Кодер', 'Обсуждение завершено · Достигли согласия']);
    const markerTags = page.locator('.msg-group-marker:not([hidden])');
    await expect(markerTags).toHaveCount(2);
    await expect(markerTags).toHaveText(['согласен', 'согласен']);
    await expect(page.locator('.msg-group').nth(3).locator('.msg-group-content')).toHaveText('Redis добавим позже, когда упрёмся в нагрузку.');
    await expect(page.locator('.msg-group.is-moderator')).toHaveCount(0);
    await expect(page.locator('#group-status')).toBeHidden();
    await expect(input).toBeEnabled();
    await expect(page.locator('.msg-group-name').first()).toHaveText('Скаут');
  });

  test('модератор: итог выделен, служебный JSON скрыт', async ({ page }) => {
    const submit = await createGroup(page, { title: 'Итог от модератора', bots: ['Скаут', 'Кодер'], mode: 'С модератором', rounds: 2 });
    await page.locator('#gr-moderator').selectOption({ label: 'Кодер' });
    await submit.click();
    await expect(page).toHaveURL(/#\/groups\/g-\d+$/);
    await page.locator('#group-input').fill('Подведите итог');
    await page.getByRole('button', { name: 'Отправить' }).click();
    await expect(page.locator('#thread-body .system-pill[data-group-event="done"]')).toBeVisible({ timeout: 20000 });
    const summary = page.locator('.msg-group.is-summary');
    await expect(summary).toHaveCount(1);
    await expect(summary).toContainText('Итог модератора');
    await expect(summary).toContainText('Итог: берём таблицу задач');
    await expect(page.locator('#thread-body')).not.toContainText('"next"');
    // модератор (Кодер) молчит в обычных раундах: только после раунда 1 и итог после раунда 2
    await expect(page.locator('.msg-group.is-moderator')).toHaveCount(2);
    await expect(page.locator('.msg-group:not(.is-moderator) .msg-group-name')).toHaveText(['Скаут', 'Скаут']);
    await expect(page.locator('#thread-body .system-pill[data-group-event="done"]')).toContainText('Модератор подвёл итог');
    await page.evaluate(() => window.__pushGroupEvent('g-2', 'assistant_msg', {
      bot_id: 'coder', bot_name: 'Кодер', role: 'moderator',
      text: '[PASS] {"next": null, "done": true}', final: true,
    }));
    const moderatorPass = page.locator('.msg-group.is-moderator').last();
    await expect(moderatorPass.locator('.msg-group-marker')).toHaveText('пропускает ход', { timeout: 5000 });
    await expect(moderatorPass.locator('.msg-group-passed')).toHaveText('Пропустил ход');
    await expect(moderatorPass).not.toContainText('"next"');
  });

  test('только маркер PASS показывает локализованную подпись вместо пустой реплики', async ({ page }) => {
    await page.goto(`${FAST}#/groups/g-1`);
    await expect(page.locator('#group-input')).toBeEnabled();
    await page.evaluate(() => window.__pushGroupEvent('g-1', 'assistant_msg', {
      bot_id: 'scout', bot_name: 'Скаут', text: '  [pAsS] \t  ', final: true,
    }));
    const reply = page.locator('.msg-group').last();
    await expect(reply.locator('.msg-group-marker')).toHaveText('пропускает ход', { timeout: 5000 });
    await expect(reply.locator('.msg-group-passed')).toHaveText('Пропустил ход');
    await expect(reply.locator('.msg-group-passed')).toHaveCSS('font-style', 'italic');
    await expect(reply.locator('.msg-group-content')).toBeHidden();
    await expect(reply).not.toContainText('[pAsS]');
  });

  test('остановка: статус пропадает, приходит «остановлено», новых реплик нет', async ({ page }) => {
    await page.goto('/?mock=1&groupdelay=1500#/groups/g-1');
    await page.locator('#group-input').fill('Спорим');
    await page.getByRole('button', { name: 'Отправить' }).click();
    await expect(page.locator('.msg-group')).toHaveCount(1, { timeout: 10000 });
    await page.getByRole('button', { name: 'Остановить' }).click();
    await expect(page.locator('#thread-body .system-pill[data-group-event="stopped"]')).toHaveText('Обсуждение остановлено · Остановлено вами');
    await expect(page.locator('#group-status')).toBeHidden();
    await expect(page.locator('#group-input')).toBeEnabled();
    const count = await page.locator('.msg-group').count();
    await page.waitForTimeout(2500);
    expect(await page.locator('.msg-group').count()).toBe(count);
  });

  test('409 group_busy: второй запуск отклонён, в окне видно, что обсуждение уже идёт', async ({ page }) => {
    await page.goto('/?mock=1&groupdelay=400#/groups/g-1');
    await expect(page.locator('#group-input')).toBeEnabled();
    const result = await page.evaluate(async () => {
      const api = await import('/api.js');
      const first = await api.sendGroupMessage('g-1', 'раз');
      let busy = null;
      try { await api.sendGroupMessage('g-1', 'два'); } catch (err) { busy = `${err.status} ${err.code}`; }
      return { run: first.run_id, busy };
    });
    expect(result.run).toBeTruthy();
    expect(result.busy).toBe('409 group_busy');
    await expect(page.locator('#group-input')).toBeDisabled(); // поток подхватил идущий запуск
    await page.getByRole('button', { name: 'Остановить' }).click();
    await expect(page.locator('#group-input')).toBeEnabled();
    await page.goto('/?mock=1&groups=busy#/groups/g-1');
    await page.locator('#group-input').fill('привет');
    await page.getByRole('button', { name: 'Отправить' }).click();
    await expect(page.getByRole('alert')).toContainText('Обсуждение уже идёт');
  });

  test('причины остановки и «отвечает» из group_turn: все stop_reason по-человечески', async ({ page }) => {
    await page.goto(`${FAST}#/groups/g-1`);
    await expect(page.locator('#group-input')).toBeEnabled();
    const reasons = { max_rounds: 'Раунды закончились', agreed: 'Достигли согласия', moderator_done: 'Модератор подвёл итог', budget: 'Кончился бюджет токенов', stopped: 'Остановлено вами', core_restart: 'Перезапуск сервера', no_participants: 'Никто из ботов не смог ответить', 'bot_error:timeout': 'Ошибка у бота: timeout' };
    for (const [code, text] of Object.entries(reasons)) {
      await page.evaluate(async ([c]) => {
        const { __pushGroupEvent } = window;
        __pushGroupEvent('g-1', 'group_status', { run_id: 'r', status: c === 'bot_error:timeout' || c === 'core_restart' || c === 'no_participants' ? 'failed' : 'done', round: 1, stop_reason: c });
      }, [code]);
      await expect(page.locator('#thread-body .system-pill').last()).toContainText(text, { timeout: 5000 });
    }
    await page.evaluate(() => {
      window.__pushGroupEvent('g-1', 'group_status', { run_id: 'r2', status: 'running', round: 2 });
      window.__pushGroupEvent('g-1', 'group_turn', { run_id: 'r2', round: 2, bot_id: 'coder', bot_name: 'Кодер' });
    });
    await expect(page.locator('#group-status')).toContainText('Идёт раунд 2 из 3 · отвечает Кодер');
  });

  test('удаление обсуждения с подтверждением', async ({ page }) => {
    await page.goto(`${FAST}#/groups/g-1`);
    await page.getByRole('button', { name: 'Удалить обсуждение' }).click();
    await page.getByRole('button', { name: 'Удалить', exact: true }).click();
    await expect(page).toHaveURL(/#\/groups$/);
    await expect(page.getByText('Обсуждений пока нет')).toBeVisible();
  });
});

test.describe('английский интерфейс', () => {
  test.beforeEach(async ({ page }) => {
    await page.addInitScript(() => { try { localStorage.setItem('bothub.lang', 'en'); } catch { /* приватный режим */ } });
  });

  test('список, форма и тред переведены, имена и реплики ботов остаются как есть', async ({ page }) => {
    await page.goto(`${FAST}&i18n=debug#/groups`);
    await expect(page.getByRole('heading', { name: 'Discussions' })).toBeVisible();
    await expect(page.locator('.group-row').first()).toContainText('Not started yet');
    await expect(page.locator('.group-row').first()).toContainText('Debate');
    await page.goto(`${FAST}&i18n=debug#/groups/new`);
    await expect(page.getByRole('heading', { name: 'New discussion' })).toBeVisible();
    await expect(page.getByLabel('Name')).toBeVisible();
    await expect(page.getByRole('radio', { name: /With moderator/ })).toContainText('After each round the moderator speaks');
    await expect(page.getByRole('button', { name: 'Create discussion' })).toBeDisabled();
    await page.goto(`${FAST}&i18n=debug#/groups/g-1`);
    await expect(page.locator('#group-input')).toHaveAttribute('placeholder', 'Topic or question to discuss');
    await page.locator('#group-input').fill('Which stack?');
    await page.getByRole('button', { name: 'Send' }).click();
    await expect(page.locator('#group-status')).toContainText('Round 1 of 3 in progress');
    await expect(page.getByRole('button', { name: 'Stop' })).toBeVisible();
    await expect(page.locator('.group-divider').first()).toHaveText('Round 1 of 3');
    await expect(page.locator('#thread-body .system-pill[data-group-event="done"]')).toHaveText('Discussion finished · The bots agreed', { timeout: 15000 });
    await expect(page.locator('.msg-group-name').first()).toHaveText('Скаут');
    await expect(page.locator('.msg-group').first()).toContainText('Предлагаю начать с простого');
    await expect(page.locator('.msg-group-marker:not([hidden])').first()).toHaveText('agrees');
    await page.evaluate(() => window.__pushGroupEvent('g-1', 'assistant_msg', {
      bot_id: 'scout', bot_name: 'Скаут', text: '[PASS]', final: true,
    }));
    const passReply = page.locator('.msg-group').last();
    await expect(passReply.locator('.msg-group-marker')).toHaveText('passes');
    await expect(passReply.locator('.msg-group-passed')).toHaveText('Passed the turn');
    const missing = await page.evaluate(() => window.__i18nMissing);
    const own = (missing || []).filter((m) => /обсужд|раунд|Остановить|Участники|Режим|Модератор/i.test(String(m)));
    expect(own).toEqual([]);
  });
});

test.describe('вёрстка', () => {
  test('тред и форма не уезжают по горизонтали, поле ввода и кнопки в экране', async ({ page }, testInfo) => {
    const size = page.viewportSize();
    for (const hash of ['#/groups', '#/groups/new', '#/groups/g-1']) {
      await page.goto(`${FAST}${hash}`);
      await expect(page.locator('#app h1').first()).toBeVisible();
      const overflow = await page.evaluate(() => Array.from(document.querySelectorAll('#app *')).filter((el) => {
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.right > window.innerWidth + 1;
      }).map((el) => `${el.tagName}.${el.className}`));
      expect(overflow, hash).toEqual([]);
    }
    await page.locator('#group-input').fill('Проверка вёрстки');
    await page.getByRole('button', { name: 'Отправить' }).click();
    const input = await page.locator('#group-input').boundingBox();
    expect(input.y + input.height).toBeLessThanOrEqual(size.height);
    const stop = page.getByRole('button', { name: 'Остановить' });
    await expect(stop).toBeVisible();
    const box = await stop.boundingBox();
    expect(box.height).toBeGreaterThanOrEqual(36);
    expect(box.x + box.width).toBeLessThanOrEqual(size.width);
    if (isMobile(testInfo)) await expect(page.locator('.screen .app-header')).toBeVisible();
    await stop.click();
  });
});
