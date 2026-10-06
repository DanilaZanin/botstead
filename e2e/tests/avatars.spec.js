import { expect, test } from '@playwright/test';

// Аватары ботов: 10 растровых персонажей (pwa/avatars/<вид>.webp), случайный выбор в мастере нового бота.
// Порядок видов совпадает с AVATAR_KINDS в pwa/avatars.js: от него зависит подмена генератора случайных чисел.
const KINDS = ['scout', 'mac', 'sre', 'coder', 'archive', 'owl', 'spark', 'robot', 'fox', 'cat'];
const DESC = 'Каждый день проверяй новые вакансии SRE и присылай краткий список';

// Подмена crypto.getRandomValues для randomAvatar: очередь значений для Uint32Array(1), остальные вызовы настоящие.
async function fakeRandom(page, values) {
  await page.addInitScript((queue) => {
    const real = crypto.getRandomValues.bind(crypto);
    crypto.getRandomValues = (arr) => {
      if (arr instanceof Uint32Array && arr.length === 1 && queue.length) { arr[0] = queue.shift(); return arr; }
      return real(arr);
    };
  }, values);
}

async function toStep2(page) {
  await page.goto(`/?mock=1#/bots/new?d=${encodeURIComponent(DESC)}`);
  await page.getByRole('button', { name: 'Собрать' }).click();
  await expect(page.getByLabel('Имя')).toBeVisible({ timeout: 5000 });
}

const big = (page) => page.locator('#bn-avatar-big img');
const pick = (page, label) => page.getByRole('button', { name: `Персонаж: ${label}` });
const bigKind = async (page) => (await big(page).getAttribute('src')).match(/avatars\/(\w+)\.webp$/)[1];

test.describe('мастер нового бота: случайный персонаж', () => {
  test('черновик получает случайного персонажа из генератора, а не предложенного конструктором', async ({ page }) => {
    await fakeRandom(page, [8]); // 8 % 10 → fox
    await toStep2(page);
    await expect(big(page)).toHaveAttribute('src', './avatars/fox.webp');
    await expect(pick(page, 'Лис')).toHaveAttribute('aria-pressed', 'true');
    await expect(page.locator('[data-bn-avatar][aria-pressed="true"]')).toHaveCount(1);
  });

  test('без подмены выпадает один из десяти видов', async ({ page }) => {
    await toStep2(page);
    expect(KINDS).toContain(await bigKind(page));
    await expect(page.locator('[data-bn-avatar][aria-pressed="true"]')).toHaveCount(1);
  });

  test('сетка выбора: 10 кнопок по 5 в ряд, не меньше 44 px, помещаются в экран', async ({ page }) => {
    await toStep2(page);
    const boxes = await page.locator('[data-bn-avatar]').evaluateAll((els) => els.map((el) => {
      const r = el.getBoundingClientRect();
      return { x: r.x, y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height), pressed: el.getAttribute('aria-pressed') };
    }));
    expect(boxes).toHaveLength(10);
    expect(boxes.filter((b) => b.w < 44 || b.h < 44)).toEqual([]);
    expect(boxes.every((b) => b.pressed === 'true' || b.pressed === 'false')).toBe(true);
    expect(new Set(boxes.slice(0, 5).map((b) => b.y)).size).toBe(1);
    expect(new Set(boxes.slice(5).map((b) => b.y)).size).toBe(1);
    expect(boxes[5].y).toBeGreaterThan(boxes[0].y);
    const width = page.viewportSize().width;
    expect(boxes.filter((b) => b.x < 0 || b.x + b.w > width)).toEqual([]);
  });

  test('«Другой» ставит иного персонажа, не равного прежнему, и не уводит фокус', async ({ page }) => {
    // 8 из 10 → fox; затем 8 из 9 видов без fox → cat (без исключения снова вышел бы fox); затем 0 из видов без cat → scout
    await fakeRandom(page, [8, 8, 0]);
    await toStep2(page);
    const again = page.getByRole('button', { name: 'Выбрать другого персонажа случайно' });
    await expect(again).toBeVisible();
    const box = await again.boundingBox();
    expect(box.width).toBeGreaterThanOrEqual(44);
    expect(box.height).toBeGreaterThanOrEqual(44);
    await expect(big(page)).toHaveAttribute('src', './avatars/fox.webp');

    await again.click();
    await expect(big(page)).toHaveAttribute('src', './avatars/cat.webp');
    await expect(pick(page, 'Кот')).toHaveAttribute('aria-pressed', 'true');
    await expect(pick(page, 'Лис')).toHaveAttribute('aria-pressed', 'false');
    await expect(page.locator('#bn-avatar-live')).toHaveText('Персонаж: Кот');
    await expect(again).toBeFocused();

    await again.click();
    await expect(big(page)).toHaveAttribute('src', './avatars/scout.webp');
  });

  test('«Другой» много раз подряд никогда не повторяет текущего', async ({ page }) => {
    await toStep2(page);
    let prev = await bigKind(page);
    const again = page.getByRole('button', { name: 'Выбрать другого персонажа случайно' });
    for (let i = 0; i < 12; i++) {
      await again.click();
      await expect.poll(() => bigKind(page)).not.toBe(prev);
      prev = await bigKind(page);
      expect(KINDS).toContain(prev);
    }
  });

  test('выбор из сетки не сбрасывается перерисовкой, возвратом на шаг 1 и повторной сборкой', async ({ page }) => {
    await fakeRandom(page, [8]);
    await toStep2(page);
    await pick(page, 'Сова').click();
    await expect(big(page)).toHaveAttribute('src', './avatars/owl.webp');
    await expect(pick(page, 'Сова')).toHaveAttribute('aria-pressed', 'true');
    // перерисовка мастера (смена места работы перерисовывает экран целиком)
    await page.getByRole('radio', { name: 'Mac', exact: true }).click();
    await expect(big(page)).toHaveAttribute('src', './avatars/owl.webp');
    await expect(pick(page, 'Сова')).toHaveAttribute('aria-pressed', 'true');
    // назад к описанию и снова «Собрать»: черновик собран заново, персонаж остаётся
    await page.getByRole('button', { name: 'Назад' }).click();
    await expect(page.getByLabel('Что бот должен делать и как работать')).toHaveValue(DESC);
    await page.getByRole('button', { name: 'Собрать' }).click();
    await expect(page.getByLabel('Имя')).toBeVisible({ timeout: 5000 });
    await expect(big(page)).toHaveAttribute('src', './avatars/owl.webp');
    await expect(pick(page, 'Сова')).toHaveAttribute('aria-pressed', 'true');
    expect(await page.evaluate(() => sessionStorage.getItem('bothub_new_bot_avatar'))).toBe('owl');
  });

  test('выбранный ключ уходит в создаваемого бота, черновик персонажа после создания стирается', async ({ page }) => {
    await fakeRandom(page, [8]);
    await toStep2(page);
    await pick(page, 'Кот').click();
    expect(await page.evaluate(() => sessionStorage.getItem('bothub_new_bot_avatar'))).toBe('cat');
    await page.getByRole('button', { name: 'Создать', exact: true }).click();
    await expect(page).toHaveURL(/#\/threads\/t-draft-/);
    expect(await page.evaluate(() => sessionStorage.getItem('bothub_new_bot_avatar'))).toBeNull();
    // мок-режим сохраняет тело createBot как есть, настройки созданного бота показывают, какой ключ ушёл
    await page.getByRole('link', { name: 'Настройки бота', exact: true }).click();
    await expect(pick(page, 'Кот')).toHaveAttribute('aria-pressed', 'true');
    await expect(page.locator('.avatar-pick[aria-pressed="true"]')).toHaveCount(1);
  });

  test('случайный выбор без правки сетки тоже доходит до созданного бота', async ({ page }) => {
    await fakeRandom(page, [8, 0]); // fox, затем «Другой» без fox → scout
    await toStep2(page);
    await page.getByRole('button', { name: 'Выбрать другого персонажа случайно' }).click();
    await expect(big(page)).toHaveAttribute('src', './avatars/scout.webp');
    await page.getByRole('button', { name: 'Создать', exact: true }).click();
    await expect(page).toHaveURL(/#\/threads\/t-draft-/);
    await page.getByRole('link', { name: 'Настройки бота', exact: true }).click();
    await expect(pick(page, 'Скаут')).toHaveAttribute('aria-pressed', 'true');
  });
});

test.describe('настройки бота и списки: картинки', () => {
  test('сетка в настройках: 10 кнопок, одна нажата, в каждой своя картинка', async ({ page }) => {
    await page.goto('/?mock=1#/bots/scout');
    await expect(page.getByText('Модель', { exact: true })).toBeVisible();
    const picks = page.locator('.avatar-pick');
    await expect(picks).toHaveCount(10);
    await expect(page.locator('.avatar-pick[aria-pressed="true"]')).toHaveCount(1);
    await expect(pick(page, 'Скаут')).toHaveAttribute('aria-pressed', 'true');
    const srcs = await picks.locator('img').evaluateAll((els) => els.map((el) => el.getAttribute('src')));
    expect(srcs).toEqual(KINDS.map((k) => `./avatars/${k}.webp`));
    for (const label of ['Лис', 'Кот']) await expect(pick(page, label)).toBeVisible();
    await pick(page, 'Лис').click();
    await expect(pick(page, 'Лис')).toHaveAttribute('aria-pressed', 'true');
    await expect(pick(page, 'Скаут')).toHaveAttribute('aria-pressed', 'false');
    await expect(page.locator('[data-avatar-slot="scout"] img').first()).toHaveAttribute('src', './avatars/fox.webp');
  });

  test('нет битых картинок аватаров на списке ботов и в сетке настроек', async ({ page }) => {
    await page.goto('/?mock=1');
    await expect(page.locator('[data-action="open-thread"][data-bot="scout"]')).toBeVisible();
    async function expectLoaded(selector, minimum) {
      const imgs = page.locator(selector);
      const count = await imgs.count();
      expect(count).toBeGreaterThanOrEqual(minimum);
      for (let i = 0; i < count; i++) {
        const img = imgs.nth(i);
        await img.scrollIntoViewIfNeeded(); // loading="lazy": ниже экрана картинка не грузится
        await expect.poll(() => img.evaluate((el) => (el.complete ? el.naturalWidth : 0)), { message: await img.getAttribute('src') }).toBeGreaterThan(0);
      }
    }
    await expectLoaded('img[src*="avatars/"]:visible', 5);
    await page.goto('/?mock=1#/bots/scout');
    await expect(page.locator('.avatar-pick')).toHaveCount(10);
    await expectLoaded('.avatar-pick img', 10);
  });

  test('все десять файлов отдаются и лежат в кэше оболочки service worker', async ({ page }) => {
    for (const kind of KINDS) {
      const res = await page.request.get(`/avatars/${kind}.webp`);
      expect(res.status(), kind).toBe(200);
      expect((await res.body()).length, kind).toBeGreaterThan(500);
    }
    const sw = await (await page.request.get('/sw.js')).text();
    for (const kind of KINDS) expect(sw).toContain(`'./avatars/${kind}.webp'`);
    expect(sw).not.toContain("'bothub-shell-v11'");
  });

  test('неизвестный ключ с сервера даёт робота и не попадает в адрес картинки', async ({ page }) => {
    await page.goto('/?mock=1');
    const html = await page.evaluate(async () => {
      const { avatarHtml, avatarLabel } = await import('./avatars.js');
      return {
        evil: avatarHtml('x.webp" onerror="alert(1)', null, 44),
        proto: avatarHtml('__proto__', null, 44),
        ctor: avatarHtml('constructor', null, 44),
        empty: avatarHtml(undefined, 'claude', 36),
        label: avatarLabel('nope'),
      };
    });
    for (const key of ['evil', 'proto', 'ctor', 'empty']) {
      expect(html[key], key).toContain('src="./avatars/robot.webp"');
      expect(html[key], key).not.toContain('onerror');
      expect(html[key], key).toContain('aria-hidden="true"');
    }
    expect(html.empty).toContain('var(--claude-fg)');
    expect(html.label).toBe('Робот: универсальный');
  });
});
