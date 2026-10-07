import { expect, test } from '@playwright/test';

// Тесты индикатора заполнения контекста и сжатия треда (этап 8): кольцо с процентами в шапке, цвета по порогам 70 и 90,
// выпадающая панель, ручное и автоматическое сжатие, сводка, вставки в ленте, порог автосжатия в настройках бота.
// Мок задаётся параметрами адреса (&ctx=<проценты>, &ctx=none, &ctxwin=none, &ctxest=1, &ctxreduce=<проценты>, &ctxfail=1)
// и хелпером window.__ctxMock (см. pwa/api.js). Окно мока 200 000 токенов, обычный ход добавляет 2 %, сжатие уменьшает на 75 %.
// Тот же файл идёт в обоих проектах Playwright: телефон 393 px и Mac 1280 px.

const T = 't-scout';

const openThread = (page, query = '', thread = T) => page.goto(`/?mock=1${query}#/threads/${thread}`);
const indicator = (page, label) => page.getByRole('button', { name: label, exact: true });
const panelOf = (page) => page.getByRole('dialog', { name: 'Контекст диалога' });
const compactButton = (page) => panelOf(page).getByRole('button', { name: /Сжать сейчас|Сжимаю/ });
const dividers = (page, kind) => page.locator(`.ctx-divider[data-ctx-event="${kind}"]`);

async function openPanel(page, label) {
  await indicator(page, label).click();
  await expect(panelOf(page)).toBeVisible();
}

// Сообщение через поле ввода: поле открывается после подключения к потоку треда.
async function sendMessage(page, text) {
  const input = page.locator('#composer-input');
  await expect(input).toBeEnabled();
  await input.fill(text);
  await page.locator('[data-action="send-message"]').click();
}

test.describe('индикатор контекста в шапке треда', () => {
  test('показывает проценты, подпись для скринридера и кольцо', async ({ page }) => {
    await openThread(page, '&ctx=42');
    const button = indicator(page, 'Контекст заполнен на 42%');
    await expect(button).toBeVisible();
    await expect(button).toContainText('42%');
    await expect(button).toHaveAttribute('aria-haspopup', 'dialog');
    await expect(button).toHaveAttribute('aria-expanded', 'false');
    await expect(button).toHaveAttribute('aria-controls', 'ctx-panel');
    await expect(button.locator('.ctx-ring-fill')).toHaveCount(1);
    await expect(page.locator('.ctx')).toHaveClass(/ctx-ok/);
  });

  for (const [percent, level] of [[0, 'ok'], [69, 'ok'], [70, 'warn'], [89, 'warn'], [90, 'crit'], [100, 'crit']]) {
    test(`заполнение ${percent} % даёт уровень «${level}»`, async ({ page }) => {
      await openThread(page, `&ctx=${percent}`);
      await expect(indicator(page, `Контекст заполнен на ${percent}%`)).toBeVisible();
      await expect(page.locator('.ctx')).toHaveClass(new RegExp(`ctx-${level}(\\s|$)`));
      for (const other of ['ok', 'warn', 'crit'].filter((x) => x !== level)) {
        await expect(page.locator('.ctx')).not.toHaveClass(new RegExp(`ctx-${other}(\\s|$)`));
      }
    });
  }

  test('три уровня различаются цветом текста и кольца', async ({ page }) => {
    const colors = [];
    for (const percent of [10, 75, 95]) {
      await openThread(page, `&ctx=${percent}`);
      await expect(indicator(page, `Контекст заполнен на ${percent}%`)).toBeVisible();
      colors.push(await page.evaluate(() => ({
        text: getComputedStyle(document.querySelector('.ctx-btn')).color,
        ring: getComputedStyle(document.querySelector('.ctx-ring-fill')).stroke,
      })));
    }
    expect(new Set(colors.map((c) => c.text)).size).toBe(3);
    expect(new Set(colors.map((c) => c.ring)).size).toBe(3);
  });

  test('без окна модели показывает число токенов', async ({ page }) => {
    await openThread(page, '&ctx=38&ctxwin=none');
    const button = indicator(page, 'Контекст: 76000 токенов');
    await expect(button).toBeVisible();
    await expect(button).toContainText('76k');
    await expect(page.locator('.ctx')).toHaveClass(/ctx-ok/);
    await button.click();
    await expect(panelOf(page)).toContainText('Занято 76');
    await expect(panelOf(page)).not.toContainText(' из ');
  });

  test('нет данных: нейтральный индикатор, панель это объясняет', async ({ page }) => {
    await openThread(page, '&ctx=none');
    const button = indicator(page, 'Контекст: нет данных');
    await expect(button).toBeVisible();
    await expect(page.locator('.ctx')).toHaveClass(/ctx-ok/);
    await button.click();
    await expect(panelOf(page)).toContainText('Данных о заполнении пока нет.');
  });

  test('сенсорная цель не меньше 44 px, кнопка в шапке и не пересекается с соседями', async ({ page }) => {
    await openThread(page, '&ctx=42');
    await expect(indicator(page, 'Контекст заполнен на 42%')).toBeVisible();
    const boxes = await page.evaluate(() => {
      const rect = (el) => { const r = el.getBoundingClientRect(); return { left: r.left, right: r.right, top: r.top, bottom: r.bottom, w: r.width, h: r.height }; };
      const head = document.querySelector('.app-header, .desktop-thread-head');
      const btn = document.querySelector('.ctx-btn');
      const neighbours = Array.from(head.querySelectorAll('a, button')).filter((el) => el !== btn && !btn.contains(el) && el.offsetParent !== null).map(rect);
      return { head: rect(head), btn: rect(btn), neighbours, scrollOverflow: document.documentElement.scrollWidth > window.innerWidth };
    });
    expect(boxes.btn.w).toBeGreaterThanOrEqual(44);
    expect(boxes.btn.h).toBeGreaterThanOrEqual(44);
    expect(boxes.btn.left).toBeGreaterThanOrEqual(boxes.head.left);
    expect(boxes.btn.right).toBeLessThanOrEqual(boxes.head.right);
    const overlapping = boxes.neighbours.filter((n) => n.left < boxes.btn.right - 1 && n.right > boxes.btn.left + 1 && n.top < boxes.btn.bottom - 1 && n.bottom > boxes.btn.top + 1);
    expect(overlapping).toEqual([]);
    expect(boxes.scrollOverflow).toBe(false);
  });
});

test.describe('выпадающая панель', () => {
  test('открывается тапом: объяснение, занято токенов, время сжатия, пояснение к кнопке', async ({ page }) => {
    await openThread(page, '&ctx=38');
    await openPanel(page, 'Контекст заполнен на 38%');
    const panel = panelOf(page);
    await expect(indicator(page, 'Контекст заполнен на 38%')).toHaveAttribute('aria-expanded', 'true');
    await expect(panel).toContainText('Бот помнит диалог в пределах контекста');
    await expect(panel).toContainText(/Занято 76\s000 из 200\s000 токенов/);
    await expect(panel.locator('.ctx-est')).toHaveCount(0);
    await expect(panel).toContainText('Сжатий пока не было');
    await expect(panel).toContainText('История останется видимой, бот продолжит по краткой сводке.');
    await expect(compactButton(page)).toBeEnabled();
    await expect(panel).not.toContainText('Автосжатие в этом диалоге отключено');
  });

  test('значение из оценки помечено', async ({ page }) => {
    await openThread(page, '&ctx=38&ctxest=1');
    await openPanel(page, 'Контекст заполнен на 38%');
    await expect(panelOf(page).locator('.ctx-est')).toHaveText('оценка');
  });

  test('закрывается по Escape, фокус возвращается на кнопку', async ({ page }) => {
    await openThread(page, '&ctx=38');
    await openPanel(page, 'Контекст заполнен на 38%');
    await page.keyboard.press('Escape');
    await expect(panelOf(page)).toBeHidden();
    await expect(indicator(page, 'Контекст заполнен на 38%')).toHaveAttribute('aria-expanded', 'false');
    await expect(indicator(page, 'Контекст заполнен на 38%')).toBeFocused();
  });

  test('закрывается кликом снаружи, повторным тапом по кнопке', async ({ page }) => {
    await openThread(page, '&ctx=38');
    await openPanel(page, 'Контекст заполнен на 38%');
    await page.locator('h1, .desktop-thread-head .t-headline').first().click();
    await expect(panelOf(page)).toBeHidden();
    await expect(indicator(page, 'Контекст заполнен на 38%')).toBeFocused();

    await openPanel(page, 'Контекст заполнен на 38%');
    await indicator(page, 'Контекст заполнен на 38%').click();
    await expect(panelOf(page)).toBeHidden();
    await expect(indicator(page, 'Контекст заполнен на 38%')).toBeFocused();
  });

  test('управляется с клавиатуры: Enter открывает, фокус уходит в панель, Escape возвращает', async ({ page }) => {
    await openThread(page, '&ctx=38');
    const button = indicator(page, 'Контекст заполнен на 38%');
    await expect(button).toBeVisible();
    await page.waitForFunction(() => { const app = document.getElementById('app'); return !!app && !app.inert; });
    await button.focus();
    await page.keyboard.press('Enter');
    await expect(panelOf(page)).toBeVisible();
    await expect(panelOf(page)).toBeFocused();
    await page.keyboard.press('Tab');
    await expect(compactButton(page)).toBeFocused();
    await page.keyboard.press('Escape');
    await expect(panelOf(page)).toBeHidden();
    await expect(button).toBeFocused();
  });

  test('помещается в экран, внутри нет горизонтальной прокрутки, у кнопок высота не меньше 44 px', async ({ page }) => {
    await openThread(page, '&ctx=38');
    await openPanel(page, 'Контекст заполнен на 38%');
    const result = await page.evaluate(() => {
      const panel = document.querySelector('.ctx-panel');
      const r = panel.getBoundingClientRect();
      const buttons = Array.from(panel.querySelectorAll('button')).filter((b) => b.offsetParent !== null).map((b) => b.getBoundingClientRect().height);
      return { left: r.left, right: r.right, width: window.innerWidth, hScroll: panel.scrollWidth > panel.clientWidth, buttons, pageScroll: document.documentElement.scrollWidth > window.innerWidth };
    });
    expect(result.left).toBeGreaterThanOrEqual(0);
    expect(result.right).toBeLessThanOrEqual(result.width);
    expect(result.hScroll).toBe(false);
    expect(result.pageScroll).toBe(false);
    expect(result.buttons.length).toBeGreaterThan(0);
    expect(result.buttons.filter((h) => h < 44)).toEqual([]);
  });

  test('атрибуты доступности: диалог с названием, статус сжатия aria-live polite', async ({ page }) => {
    await openThread(page, '&ctx=38');
    await openPanel(page, 'Контекст заполнен на 38%');
    await expect(panelOf(page)).toHaveAttribute('role', 'dialog');
    const status = panelOf(page).locator('.ctx-status');
    await expect(status).toHaveAttribute('role', 'status');
    await expect(status).toHaveAttribute('aria-live', 'polite');
  });
});

test.describe('ручное сжатие', () => {
  test('кнопка сжимает: разделитель в ленте, процент упал, история на месте, пустых пузырей нет', async ({ page }) => {
    await openThread(page, '&ctx=76');
    await expect(page.locator('#thread-body')).toContainText('Найди вакансии SRE remote в Европе');
    const before = await page.evaluate(() => ({ user: document.querySelectorAll('.msg-user').length, bot: document.querySelectorAll('.msg-bot').length }));
    await openPanel(page, 'Контекст заполнен на 76%');
    await compactButton(page).click();
    // Пока идёт сжатие, кнопка недоступна и подписана «Сжимаю…»
    await expect(compactButton(page)).toBeDisabled();
    await expect(compactButton(page)).toHaveText('Сжимаю…');
    await expect(panelOf(page).locator('.ctx-status')).toContainText('Сжимаю контекст…');

    const divider = dividers(page, 'compacted');
    await expect(divider).toHaveCount(1, { timeout: 15_000 });
    await expect(divider).toHaveText('Контекст сжат: было 152k токенов, стало 38k токенов');
    await expect(indicator(page, 'Контекст заполнен на 19%')).toBeVisible();
    await expect(page.locator('.ctx')).toHaveClass(/ctx-ok/);
    await expect(compactButton(page)).toBeEnabled();
    await expect(compactButton(page)).toHaveText('Сжать сейчас');
    await expect(panelOf(page).locator('.ctx-status')).toHaveText('Контекст сжат');
    await expect(panelOf(page)).toContainText(/Последнее сжатие: \S/);
    await expect(panelOf(page).locator('.ctx-est')).toHaveText('оценка');

    // История осталась видимой, ход сжатия не добавил пузырей и строк расхода
    await expect(page.locator('#thread-body')).toContainText('Найди вакансии SRE remote в Европе');
    const after = await page.evaluate(() => ({
      user: document.querySelectorAll('.msg-user').length,
      bot: document.querySelectorAll('.msg-bot').length,
      emptyBubbles: Array.from(document.querySelectorAll('.msg-bot, .msg-user')).filter((el) => !el.textContent.trim()).length,
    }));
    expect(after.user).toBe(before.user);
    expect(after.bot).toBe(before.bot);
    expect(after.emptyBubbles).toBe(0);
  });

  test('цвет индикатора возвращается из критического уровня после сжатия', async ({ page }) => {
    await openThread(page, '&ctx=93');
    await expect(page.locator('.ctx')).toHaveClass(/ctx-crit/);
    await openPanel(page, 'Контекст заполнен на 93%');
    await compactButton(page).click();
    await expect(indicator(page, 'Контекст заполнен на 23%')).toBeVisible({ timeout: 15_000 });
    await expect(page.locator('.ctx')).toHaveClass(/ctx-ok/);
  });

  test('кнопка недоступна, пока в треде идёт ход, и включается после его конца', async ({ page }) => {
    await openThread(page, '&ctx=38');
    await expect(indicator(page, 'Контекст заполнен на 38%')).toBeVisible();
    await page.evaluate((id) => window.__ctxMock.busy(id, true), T);
    await openPanel(page, 'Контекст заполнен на 38%');
    await expect(compactButton(page)).toBeDisabled({ timeout: 8_000 });
    await expect(panelOf(page)).toContainText('Пока бот отвечает, сжать нельзя.');
    await page.evaluate((id) => window.__ctxMock.busy(id, false), T);
    await expect(compactButton(page)).toBeEnabled({ timeout: 8_000 });
    await expect(panelOf(page)).not.toContainText('Пока бот отвечает, сжать нельзя.');
  });

  test('обычный ход двигает индикатор, после хода кнопка снова доступна', async ({ page }) => {
    await openThread(page, '');
    await expect(indicator(page, 'Контекст заполнен на 12%')).toBeVisible();
    await sendMessage(page, 'Привет, это проверка');
    await expect(indicator(page, 'Контекст заполнен на 14%')).toBeVisible({ timeout: 15_000 });
    await openPanel(page, 'Контекст заполнен на 14%');
    await expect(compactButton(page)).toBeEnabled();
    await expect(page.locator('#thread-body')).toContainText('ok: Привет, это проверка');
  });

  test('при 409 показывает понятную ошибку, кнопка остаётся доступной', async ({ page }) => {
    await openThread(page, '&ctx=38');
    await expect(indicator(page, 'Контекст заполнен на 38%')).toBeVisible();
    // сервер считает тред занятым, а клиент об этом ещё не знает (события нет)
    await page.evaluate((id) => window.__ctxMock.set(id, { activeTurn: 'turn-hidden' }, { notify: false }), T);
    await openPanel(page, 'Контекст заполнен на 38%');
    await compactButton(page).click();
    const status = panelOf(page).locator('.ctx-status');
    await expect(status).toContainText('Сейчас сжать нельзя: бот занят или в диалоге пока нечего сжимать.');
    await expect(status).toHaveClass(/is-error/);
    await expect(compactButton(page)).toBeEnabled();
    await expect(dividers(page, 'compacted')).toHaveCount(0);
  });

  test('при 409 из-за пустого треда (нет данных) показывает ту же ошибку', async ({ page }) => {
    await openThread(page, '&ctx=none');
    await openPanel(page, 'Контекст: нет данных');
    await compactButton(page).click();
    await expect(panelOf(page).locator('.ctx-status')).toContainText('Сейчас сжать нельзя');
  });

  test('сбой сжатия: нейтральная строка в ленте, контекст не изменился', async ({ page }) => {
    await openThread(page, '&ctx=76&ctxfail=1');
    await openPanel(page, 'Контекст заполнен на 76%');
    await compactButton(page).click();
    const failed = dividers(page, 'compact_failed');
    await expect(failed).toHaveCount(1, { timeout: 15_000 });
    await expect(failed).toContainText('Не удалось сжать контекст');
    await expect(panelOf(page).locator('.ctx-status')).toHaveText('Не удалось сжать контекст');
    await expect(indicator(page, 'Контекст заполнен на 76%')).toBeVisible();
    await expect(compactButton(page)).toBeEnabled();
  });
});

test.describe('сводка', () => {
  test('до сжатия сводки нет, после сжатия её можно показать и скрыть', async ({ page }) => {
    await openThread(page, '&ctx=76');
    await openPanel(page, 'Контекст заполнен на 76%');
    await expect(panelOf(page)).toContainText('Сводки пока нет: контекст ещё не сжимали.');
    await expect(panelOf(page).getByRole('button', { name: 'Показать сводку' })).toHaveCount(0);

    await compactButton(page).click();
    const toggle = panelOf(page).getByRole('button', { name: 'Показать сводку' });
    await expect(toggle).toBeVisible({ timeout: 15_000 });
    await expect(panelOf(page)).not.toContainText('Сводки пока нет');
    await expect(toggle).toHaveAttribute('aria-expanded', 'false');
    await expect(toggle).toHaveAttribute('aria-controls', 'ctx-summary');
    await expect(page.locator('#ctx-summary')).toBeHidden();

    await toggle.click();
    const summary = page.locator('#ctx-summary');
    await expect(summary).toBeVisible();
    await expect(summary).toContainText('Краткая сводка диалога');
    await expect(summary).toHaveAttribute('data-i18n-skip', '');
    await expect(panelOf(page).getByRole('button', { name: 'Скрыть сводку' })).toHaveAttribute('aria-expanded', 'true');
    const style = await summary.evaluate((el) => ({ whiteSpace: getComputedStyle(el).whiteSpace, maxHeight: getComputedStyle(el).maxHeight, overflowY: getComputedStyle(el).overflowY }));
    expect(style.whiteSpace).toBe('pre-wrap');
    expect(style.maxHeight).not.toBe('none');
    expect(style.overflowY).toBe('auto');

    await panelOf(page).getByRole('button', { name: 'Скрыть сводку' }).click();
    await expect(summary).toBeHidden();
  });

  test('длинная сводка прокручивается внутри блока, панель не вылезает за экран', async ({ page }) => {
    await openThread(page, '&ctx=40');
    await expect(indicator(page, 'Контекст заполнен на 40%')).toBeVisible();
    const long = Array.from({ length: 80 }, (_, i) => `Строка сводки ${i + 1}`).join('\n');
    await page.evaluate(([id, text]) => window.__ctxMock.set(id, { summary: text }), [T, long]);
    await openPanel(page, 'Контекст заполнен на 40%');
    await panelOf(page).getByRole('button', { name: 'Показать сводку' }).click({ timeout: 8_000 });
    const metrics = await page.evaluate(() => {
      const box = document.querySelector('#ctx-summary');
      const panel = document.querySelector('.ctx-panel');
      return { scrolls: box.scrollHeight > box.clientHeight, boxH: box.clientHeight, vh: window.innerHeight, panelBottom: panel.getBoundingClientRect().bottom };
    });
    expect(metrics.scrolls).toBe(true);
    expect(metrics.boxH).toBeLessThanOrEqual(metrics.vh * 0.41);
    expect(metrics.panelBottom).toBeLessThanOrEqual(metrics.vh);
  });

  test('HTML в тексте сводки показывается как текст', async ({ page }) => {
    await openThread(page, '&ctx=40');
    await expect(indicator(page, 'Контекст заполнен на 40%')).toBeVisible();
    const evil = '<b>теги</b> и <img src=x onerror="window.__ctxXss=1"> & "кавычки"';
    await page.evaluate(([id, text]) => window.__ctxMock.set(id, { summary: text }), [T, evil]);
    await openPanel(page, 'Контекст заполнен на 40%');
    await panelOf(page).getByRole('button', { name: 'Показать сводку' }).click({ timeout: 8_000 });
    const summary = page.locator('#ctx-summary');
    await expect(summary).toHaveText(evil);
    await expect(summary.locator('b, img')).toHaveCount(0);
    expect(await page.evaluate(() => window.__ctxXss)).toBeUndefined();
  });
});

test.describe('вставки в ленте', () => {
  test('экранирует деталь сбоя и показывает её текстом', async ({ page }) => {
    await openThread(page, '&ctx=40');
    await expect(indicator(page, 'Контекст заполнен на 40%')).toBeVisible();
    const evil = '<img src=x onerror="window.__ctxXss2=1">сбой';
    await page.evaluate(([id, detail]) => window.__ctxMock.push(id, 'compact_failed', { detail }, 'turn-x'), [T, evil]);
    const failed = dividers(page, 'compact_failed');
    await expect(failed).toHaveCount(1, { timeout: 8_000 });
    await expect(failed).toContainText('Не удалось сжать контекст');
    await expect(failed).toContainText(evil);
    await expect(failed.locator('img')).toHaveCount(0);
    expect(await page.evaluate(() => window.__ctxXss2)).toBeUndefined();
  });

  test('варианты текста разделителя: было и стало, без «было», автоматически', async ({ page }) => {
    await openThread(page, '&ctx=40');
    await expect(indicator(page, 'Контекст заполнен на 40%')).toBeVisible();
    await page.evaluate((id) => {
      window.__ctxMock.push(id, 'compacted', { tokens_before: 84000, tokens_after: 21000, auto: false, reduction_percent: 75, summary_chars: 100 }, 'turn-a');
      window.__ctxMock.push(id, 'compacted', { tokens_before: 84000, tokens_after: 21000, auto: true, reduction_percent: 75, summary_chars: 100 }, 'turn-b');
      window.__ctxMock.push(id, 'compacted', { tokens_before: null, tokens_after: 900, auto: false, reduction_percent: null, summary_chars: 100 }, 'turn-c');
      window.__ctxMock.push(id, 'compacted', { tokens_before: null, tokens_after: 4200, auto: true, reduction_percent: null, summary_chars: 100 }, 'turn-d');
      window.__ctxMock.push(id, 'auto_compact_disabled', { reduction_percent: 4 }, 'turn-d');
    }, T);
    const all = dividers(page, 'compacted');
    await expect(all).toHaveCount(4, { timeout: 8_000 });
    await expect(all.nth(0)).toHaveText('Контекст сжат: было 84k токенов, стало 21k токенов');
    await expect(all.nth(1)).toHaveText('Контекст сжат автоматически: было 84k токенов, стало 21k токенов');
    await expect(all.nth(2)).toHaveText('Контекст сжат, сейчас 900 токенов');
    await expect(all.nth(3)).toHaveText('Контекст сжат автоматически, сейчас 4k токенов');
    await expect(dividers(page, 'auto_compact_disabled')).toHaveText('Автосжатие отключено: сжатие почти не помогло');
  });

  test('разделитель остаётся в истории после перезагрузки экрана', async ({ page }) => {
    await openThread(page, '&ctx=40');
    await expect(indicator(page, 'Контекст заполнен на 40%')).toBeVisible();
    await page.evaluate((id) => window.__ctxMock.push(id, 'compacted', { tokens_before: 84000, tokens_after: 21000, auto: false, reduction_percent: 75, summary_chars: 100 }, 'turn-h'), T);
    await expect(dividers(page, 'compacted')).toHaveCount(1, { timeout: 8_000 });
    // уходим на главную и возвращаемся: лента читается заново из событий
    await page.evaluate(() => { location.hash = '#/'; });
    await expect(page.locator('#thread-body')).toHaveCount(0);
    await page.evaluate((id) => { location.hash = `#/threads/${id}`; }, T);
    await expect(dividers(page, 'compacted')).toHaveCount(1);
    await expect(dividers(page, 'compacted')).toHaveText('Контекст сжат: было 84k токенов, стало 21k токенов');
  });
});

test.describe('автосжатие', () => {
  test('сжимает по порогу бота после хода, не чаще раза в 3 хода', async ({ page }) => {
    await openThread(page, '&ctx=79');
    await expect(indicator(page, 'Контекст заполнен на 79%')).toBeVisible();
    await sendMessage(page, 'Первое сообщение');
    // 79 % + 2 % за ход = 81 % ≥ порога 80 %: бот сам сжимает контекст
    const auto = dividers(page, 'compacted');
    await expect(auto).toHaveCount(1, { timeout: 20_000 });
    await expect(auto).toHaveText('Контекст сжат автоматически: было 162k токенов, стало 41k токенов');
    await expect(indicator(page, 'Контекст заполнен на 20%')).toBeVisible({ timeout: 8_000 });
    await expect(page.locator('.ctx')).toHaveClass(/ctx-ok/);

    // сразу поднимаем заполнение выше порога: новый ход не вызывает второго сжатия, пока не прошло 3 хода
    await page.evaluate((id) => window.__ctxMock.setPercent(id, 90, { notify: false }), T);
    await sendMessage(page, 'Второе сообщение');
    await expect(indicator(page, 'Контекст заполнен на 92%')).toBeVisible({ timeout: 15_000 });
    await page.waitForTimeout(2500);
    await expect(dividers(page, 'compacted')).toHaveCount(1);
    await expect(indicator(page, 'Контекст заполнен на 92%')).toBeVisible();
  });

  test('автосжатие выключено у бота: заполнение растёт без сжатия', async ({ page }) => {
    await page.goto('/?mock=1&ctx=79#/bots/scout');
    await page.locator('#ac-switch').click();
    await expect(page.locator('#ac-switch')).not.toBeChecked();
    await expect.poll(() => page.evaluate(() => window.__ctxMock.bot('scout').auto_compact_percent)).toBe(null);
    await page.evaluate(() => { location.hash = '#/threads/t-scout'; });
    await expect(indicator(page, 'Контекст заполнен на 79%')).toBeVisible();
    await sendMessage(page, 'Сообщение без автосжатия');
    await expect(indicator(page, 'Контекст заполнен на 81%')).toBeVisible({ timeout: 15_000 });
    await page.waitForTimeout(2500);
    await expect(dividers(page, 'compacted')).toHaveCount(0);
  });

  test('слабое сжатие отключает автосжатие: строка в ленте и пометка в панели', async ({ page }) => {
    await openThread(page, '&ctx=79&ctxreduce=5');
    await expect(indicator(page, 'Контекст заполнен на 79%')).toBeVisible();
    await sendMessage(page, 'Сообщение');
    const off = dividers(page, 'auto_compact_disabled');
    await expect(off).toHaveCount(1, { timeout: 20_000 });
    await expect(off).toHaveText('Автосжатие отключено: сжатие почти не помогло');
    await expect(dividers(page, 'compacted')).toHaveCount(1);
    // 162 000 токенов сжались на 5 %: 153 900 из 200 000
    await openPanel(page, 'Контекст заполнен на 76%');
    await expect(panelOf(page)).toContainText('Автосжатие в этом диалоге отключено');
  });
});

test.describe('порог автосжатия в настройках бота', () => {
  test('по умолчанию включён и стоит на 80 %, порог сохраняется', async ({ page }) => {
    await page.goto('/?mock=1#/bots/scout');
    const toggle = page.locator('#ac-switch');
    const range = page.locator('#ac-range');
    await expect(page.getByText('Автосжатие контекста')).toBeVisible();
    await expect(toggle).toBeChecked();
    await expect(range).toBeEnabled();
    await expect(range).toHaveValue('80');
    await expect(range).toHaveAttribute('min', '50');
    await expect(range).toHaveAttribute('max', '95');
    await expect(range).toHaveAttribute('step', '5');
    await expect(page.locator('[data-ac-value]')).toHaveText('80%');

    await range.fill('90');
    await expect(page.locator('[data-ac-value]')).toHaveText('90%');
    await expect.poll(() => page.evaluate(() => window.__ctxMock.bot('scout').auto_compact_percent)).toBe(90);
    await expect(page.locator('#bot-alert [role="alert"]')).toHaveCount(0);

    // экран перерисовывается из ядра: значение на месте
    await page.evaluate(() => { location.hash = '#/'; });
    await expect(page.locator('#ac-range')).toHaveCount(0);
    await page.evaluate(() => { location.hash = '#/bots/scout'; });
    await expect(page.locator('#ac-range')).toHaveValue('90');
    await expect(page.locator('[data-ac-value]')).toHaveText('90%');
  });

  test('выключатель отправляет null и блокирует ползунок, включение возвращает порог', async ({ page }) => {
    await page.goto('/?mock=1#/bots/scout');
    const toggle = page.locator('#ac-switch');
    const range = page.locator('#ac-range');
    await expect(toggle).toBeChecked();

    await toggle.click();
    await expect(toggle).not.toBeChecked();
    await expect(range).toBeDisabled();
    await expect.poll(() => page.evaluate(() => window.__ctxMock.bot('scout').auto_compact_percent)).toBe(null);

    await page.evaluate(() => { location.hash = '#/'; });
    await expect(page.locator('#ac-switch')).toHaveCount(0);
    await page.evaluate(() => { location.hash = '#/bots/scout'; });
    await expect(page.locator('#ac-switch')).not.toBeChecked();
    await expect(page.locator('#ac-range')).toBeDisabled();

    await page.locator('#ac-switch').click();
    await expect(page.locator('#ac-switch')).toBeChecked();
    await expect(page.locator('#ac-range')).toBeEnabled();
    await expect.poll(() => page.evaluate(() => window.__ctxMock.bot('scout').auto_compact_percent)).toBe(80);
  });

  test('ползунок и переключатель доступны с клавиатуры, зона касания не меньше 44 px', async ({ page }) => {
    await page.goto('/?mock=1#/bots/scout');
    const range = page.locator('#ac-range');
    await expect(range).toBeVisible();
    await range.focus();
    await page.keyboard.press('ArrowRight');
    await expect(range).toHaveValue('85');
    await expect(page.locator('[data-ac-value]')).toHaveText('85%');
    await expect.poll(() => page.evaluate(() => window.__ctxMock.bot('scout').auto_compact_percent)).toBe(85);
    const rect = await range.evaluate((el) => { const r = el.getBoundingClientRect(); return { h: r.height, w: r.width }; });
    expect(rect.h).toBeGreaterThanOrEqual(44);
    const viewport = page.viewportSize();
    if (viewport && viewport.width <= 400) {
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
      expect(overflow).toBe(false);
    }
  });
});

test.describe('английский интерфейс', () => {
  test('подпись индикатора, тексты панели и разделитель переведены', async ({ page }) => {
    await page.addInitScript(() => { try { localStorage.setItem('bothub.lang', 'en'); } catch { /* приватный режим */ } });
    await openThread(page, '&ctx=42');
    await expect(indicator(page, 'Context 42% full')).toBeVisible();
    await indicator(page, 'Context 42% full').click();
    const panel = page.getByRole('dialog', { name: 'Conversation context' });
    await expect(panel).toBeVisible();
    await expect(panel).toContainText('The bot remembers the conversation within its context');
    await expect(panel).toContainText('Used 84');
    await expect(panel).toContainText('No compactions yet');
    await panel.getByRole('button', { name: 'Compact now' }).click();
    await expect(page.locator('.ctx-divider[data-ctx-event="compacted"]')).toHaveText('Context compacted: was 84k tokens, now 21k tokens', { timeout: 15_000 });
    await expect(indicator(page, 'Context 10% full')).toBeVisible();
    await expect(panel.getByRole('button', { name: 'Show summary' })).toBeVisible();
  });
});
