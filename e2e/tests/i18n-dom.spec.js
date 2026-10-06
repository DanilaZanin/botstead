import { expect, test } from '@playwright/test';

// DOM-переводчик pwa/i18n-dom.js: при английском интерфейсе экраны остаются на русских строках в исходниках,
// а готовый DOM переводится по каталогам pwa/i18n/en-*.js. Мок-режим: ?mock=1, отчёт о пропусках: &i18n=debug.

const MISSING_LIMIT = 400;

// Язык выставляется до загрузки приложения, как после нажатия переключателя (ключ localStorage bothub.lang).
async function useLang(page, lang) {
  await page.addInitScript((code) => {
    try { localStorage.setItem('bothub.lang', code); } catch { /* приватный режим */ }
  }, lang);
}

// Тег-последовательность экрана: по ней видно, что перевод не добавил и не убрал элементы.
const tagSequence = (page) => page.evaluate(() => Array.from(document.querySelectorAll('#app *')).map((el) => el.tagName).join(','));

const SCREENS = [
  { name: 'настройки', hash: '#/settings', english: 'Change password', russian: 'Сменить пароль' },
  { name: 'память', hash: '#/memory', english: 'Memory', russian: 'Память' },
  { name: 'решения', hash: '#/approvals', english: 'Approval', russian: 'Решения' },  // на телефоне заголовок карточки в единственном числе
  { name: 'рутины', hash: '#/routines', english: 'Routines', russian: 'Рутины' },
  { name: 'расход', hash: '#/usage', english: 'Usage', russian: 'Расход' },
  { name: 'провайдеры', hash: '#/settings/providers', english: 'Providers', russian: 'Провайдеры' },
  { name: 'тред бота', hash: '#/threads/t-scout', placeholder: [/^Message/, 'Сообщение'] },
  { name: 'главный экран', hash: '#/', english: 'Approvals', russian: 'Решения' },
];

test.describe('английский интерфейс: 8 экранов', () => {
  for (const screen of SCREENS) {
    test(`экран «${screen.name}»: английский текст, отчёт __i18nMissing не больше ${MISSING_LIMIT}`, async ({ page }) => {
      await useLang(page, 'en');
      await page.goto(`/?mock=1&i18n=debug${screen.hash}`);
      await expect(page.locator('html')).toHaveAttribute('lang', 'en');
      if (screen.placeholder) {
        // поле ввода: перевод идёт через атрибут placeholder (шаблонный ключ с именем бота)
        const [english, russian] = screen.placeholder;
        const placeholder = await page.locator('#composer-input').getAttribute('placeholder');
        expect(placeholder).toMatch(english);
        expect(placeholder).not.toContain(russian);
      } else {
        await expect(page.locator('#app')).toContainText(screen.english);
        // Перевод не должен оставлять русскую подпись экрана рядом с английской.
        const headings = await page.locator('#app h1, #app h2').allInnerTexts();
        expect(headings.join(' ')).not.toContain(screen.russian);
      }
      const missing = await page.evaluate(() => window.__i18nMissing);
      expect(Array.isArray(missing)).toBe(true);
      expect(missing.length).toBeLessThanOrEqual(MISSING_LIMIT);
    });
  }

  test('без debug-режима отчёта __i18nMissing нет', async ({ page }) => {
    await useLang(page, 'en');
    await page.goto('/?mock=1#/settings');
    await expect(page.locator('#app')).toContainText('Change password');
    expect(await page.evaluate(() => window.__i18nMissing)).toBeUndefined();
  });

  test('debug-режим включается и ключом localStorage', async ({ page }) => {
    await useLang(page, 'en');
    await page.addInitScript(() => { try { localStorage.setItem('bothub.i18n.debug', '1'); } catch { /* приватный режим */ } });
    await page.goto('/?mock=1#/settings');
    await expect(page.locator('#app')).toContainText('Change password');
    expect(Array.isArray(await page.evaluate(() => window.__i18nMissing))).toBe(true);
  });
});

test.describe('язык по умолчанию остаётся русским', () => {
  test('без сохранённого выбора интерфейс русский даже при navigator.language en-US', async ({ page }) => {
    await page.addInitScript(() => {
      try { localStorage.removeItem('bothub.lang'); } catch { /* приватный режим */ }
      Object.defineProperty(navigator, 'language', { get: () => 'en-US', configurable: true });
    });
    await page.goto('/?mock=1#/settings');
    await expect(page.locator('html')).toHaveAttribute('lang', 'ru');
    await expect(page.getByText('Сменить пароль')).toBeVisible();
    expect(await page.evaluate(() => window.__i18nMissing)).toBeUndefined();
  });
});

test.describe('что не переводится', () => {
  test('пользовательский контент в треде остаётся русским, текст интерфейса переводится', async ({ page }) => {
    await useLang(page, 'en');
    await page.goto('/?mock=1&i18n=debug#/threads/t-scout');
    const userMsg = page.locator('.msg-user').first();
    await expect(userMsg).toContainText('Найди вакансии SRE remote в Европе');
    await expect(page.locator('.msg-bot').first()).toContainText('Нашёл 7 вакансий SRE');
    const missing = await page.evaluate(() => window.__i18nMissing);
    expect(missing.some((m) => m.includes('Найди вакансии SRE'))).toBe(false);
  });

  test('data-i18n-skip, code, pre, textarea, contenteditable и поля ввода не переводятся, обычные узлы переводятся', async ({ page }) => {
    await useLang(page, 'en');
    await page.goto('/?mock=1#/settings');
    await expect(page.locator('#app')).toContainText('Change password');
    await page.evaluate(() => {
      const box = document.createElement('div');
      box.id = 'probe';
      box.innerHTML = `
        <p id="p-plain">Закрыть</p>
        <p id="p-skip" data-i18n-skip>Закрыть</p>
        <div data-i18n-skip><span id="p-skip-child">Закрыть</span><input id="p-skip-input" placeholder="Закрыть"></div>
        <code id="p-code">Закрыть</code>
        <pre id="p-pre">Закрыть</pre>
        <textarea id="p-area" placeholder="Закрыть">Закрыть</textarea>
        <div id="p-edit" contenteditable="true">Закрыть</div>
        <input id="i-plain" placeholder="Закрыть" aria-label="Закрыть" title="Закрыть">
        <input id="i-text" type="text" value="Закрыть">
        <input id="i-submit" type="submit" value="Закрыть">
        <select id="s-opt"><option id="o-1" value="Закрыть">Закрыть</option></select>
        <button id="b-tip" data-tooltip="Закрыть" alt="Закрыть">x</button>
        <img id="img-alt" alt="Закрыть" src="data:image/gif;base64,R0lGODlhAQABAAAAACw=">`;
      document.body.appendChild(box);
    });
    const text = (id) => page.locator(`#${id}`).innerText();
    await expect(page.locator('#p-plain')).toHaveText('Close');
    await expect(page.locator('#i-plain')).toHaveAttribute('placeholder', 'Close');
    await expect(page.locator('#i-plain')).toHaveAttribute('aria-label', 'Close');
    await expect(page.locator('#i-plain')).toHaveAttribute('title', 'Close');
    await expect(page.locator('#i-submit')).toHaveAttribute('value', 'Close');
    await expect(page.locator('#p-area')).toHaveAttribute('placeholder', 'Close');
    await expect(page.locator('#b-tip')).toHaveAttribute('data-tooltip', 'Close');
    await expect(page.locator('#img-alt')).toHaveAttribute('alt', 'Close');
    expect(await text('p-skip')).toBe('Закрыть');
    expect(await text('p-skip-child')).toBe('Закрыть');
    expect(await page.locator('#p-skip-input').getAttribute('placeholder')).toBe('Закрыть');
    expect(await text('p-code')).toBe('Закрыть');
    expect(await text('p-pre')).toBe('Закрыть');
    expect(await page.locator('#p-area').inputValue()).toBe('Закрыть');
    expect(await text('p-edit')).toBe('Закрыть');
    expect(await page.locator('#i-text').inputValue()).toBe('Закрыть');
    // value у option и у текстовых полей служит данными: его перевод сломал бы логику
    expect(await page.locator('#o-1').getAttribute('value')).toBe('Закрыть');
  });

  test('перевод не вносит разметку: набор тегов экрана тот же, что при русском интерфейсе', async ({ page }) => {
    await page.goto('/?mock=1#/settings');
    await expect(page.getByText('Сменить пароль')).toBeVisible();
    await page.waitForTimeout(800); // счётчик запросов дорисовывается после загрузки (у участника он скрыт)
    const ru = await tagSequence(page);
    const ruHtmlLang = await page.evaluate(() => document.documentElement.lang);
    expect(ruHtmlLang).toBe('ru');

    await useLang(page, 'en');
    await page.reload(); // тот же адрес: goto не перезагрузил бы документ, и скрипт выбора языка не сработал бы
    await expect(page.getByText('Change password')).toBeVisible();
    await page.waitForTimeout(800);
    expect(await tagSequence(page)).toBe(ru);

    // текст с угловыми скобками остаётся текстом
    await page.evaluate(() => {
      const p = document.createElement('p');
      p.id = 'markup';
      p.textContent = '<b>Закрыть</b>';
      document.body.appendChild(p);
    });
    await page.waitForTimeout(100);
    expect(await page.locator('#markup').evaluate((el) => el.children.length)).toBe(0);
    expect(await page.locator('#markup').innerText()).toBe('<b>Закрыть</b>');
  });
});

test.describe('шаблоны и динамика', () => {
  test('шаблонные ключи с числом и склонением: «3 бота без модели»', async ({ page }) => {
    await useLang(page, 'en');
    await page.goto('/?mock=1#/settings');
    await expect(page.locator('#app')).toContainText('Change password');
    await page.evaluate(() => {
      const box = document.createElement('div');
      box.id = 'tpl';
      box.innerHTML = `
        <p id="t1">1 бот без модели</p>
        <p id="t2">3 бота без модели</p>
        <p id="t5">5 ботов без модели</p>
        <p id="t-sub">Подписка · 4</p>
        <p id="t-srv">Вход на сервер example.org:8443</p>`;
      document.body.appendChild(box);
    });
    await expect(page.locator('#t1')).toHaveText('1 bot without a model');
    await expect(page.locator('#t2')).toHaveText('3 bots without a model');
    await expect(page.locator('#t5')).toHaveText('5 bots without a model');
    await expect(page.locator('#t-sub')).toHaveText('Subscription · 4');
    await expect(page.locator('#t-srv')).toHaveText('Sign in to server example.org:8443');
  });

  test('текст, изменённый приложением позже, переводится заново и не зацикливается', async ({ page }) => {
    await useLang(page, 'en');
    await page.goto('/?mock=1#/settings');
    await expect(page.locator('#app')).toContainText('Change password');
    await page.evaluate(() => {
      const p = document.createElement('p');
      p.id = 'dyn';
      p.textContent = 'Отмена';
      document.body.appendChild(p);
      window.__dynMutations = 0;
      new MutationObserver((records) => { window.__dynMutations += records.length; }).observe(p, { characterData: true, childList: true, subtree: true });
    });
    await expect(page.locator('#dyn')).toHaveText('Cancel');
    await page.evaluate(() => { document.getElementById('dyn').firstChild.data = 'Повторить'; });
    await expect(page.locator('#dyn')).toHaveText('Retry');
    await page.waitForTimeout(300);
    // два перевода и два наших изменения: счётчик не растёт после стабилизации
    const settled = await page.evaluate(() => window.__dynMutations);
    await page.waitForTimeout(300);
    expect(await page.evaluate(() => window.__dynMutations)).toBe(settled);
    expect(settled).toBeLessThanOrEqual(4);
    await expect(page.locator('#dyn')).toHaveText('Retry');
  });
});

test.describe('переключатель языка', () => {
  test('настройки: выбор English и обратно, страница перезагружается, выбор сохраняется', async ({ page }) => {
    await page.goto('/?mock=1#/settings');
    await expect(page.getByText('Сменить пароль')).toBeVisible();
    await page.getByRole('radio', { name: 'English' }).click();
    await expect(page.locator('html')).toHaveAttribute('lang', 'en');
    await expect(page.getByText('Change password')).toBeVisible();
    expect(await page.evaluate(() => localStorage.getItem('bothub.lang'))).toBe('en');
    // названия языков не переводятся
    await expect(page.getByRole('radio', { name: 'Русский' })).toBeVisible();
    await expect(page.getByRole('radio', { name: 'English' })).toHaveAttribute('aria-checked', 'true');

    await page.getByRole('radio', { name: 'Русский' }).click();
    await expect(page.locator('html')).toHaveAttribute('lang', 'ru');
    await expect(page.getByText('Сменить пароль')).toBeVisible();
    expect(await page.evaluate(() => localStorage.getItem('bothub.lang'))).toBe('ru');
  });

  test('выбор в настройках: группа находится по data-seg, а не по переведённой подписи', async ({ page }) => {
    await useLang(page, 'en');
    await page.goto('/?mock=1#/settings');
    await expect(page.getByText('Change password')).toBeVisible();
    const group = page.locator('[role="radiogroup"][data-seg="Язык интерфейса"]');
    await expect(group).toHaveAttribute('aria-label', 'Interface language');
  });

  test('вход: ссылка переключает язык', async ({ page }) => {
    await page.goto('/?mock=1&auth=none');
    await expect(page.getByText('Вход на сервер')).toBeVisible();
    await page.locator('#lang-link').click();
    await expect(page.locator('html')).toHaveAttribute('lang', 'en');
    await expect(page.getByText('Sign in to server')).toBeVisible();
    await expect(page.locator('#lang-link')).toHaveText('Русский');
    await page.locator('#lang-link').click();
    await expect(page.locator('html')).toHaveAttribute('lang', 'ru');
    await expect(page.getByText('Вход на сервер')).toBeVisible();
  });

  test('форма «Новый инвайт» читает значения групп и при английском интерфейсе', async ({ page }) => {
    await useLang(page, 'en');
    await page.goto('/?mock=1#/settings/users');
    await expect(page.locator('#app')).toContainText('Users');
    expect(await page.evaluate(async () => {
      const { segmentedHtml, segmentedValue } = await import('/account.js');
      const host = document.createElement('div');
      host.innerHTML = segmentedHtml('Роль', [{ value: 'member', label: 'Участник' }, { value: 'admin', label: 'Админ' }], 'admin');
      document.body.appendChild(host);
      await new Promise((resolve) => setTimeout(resolve, 100));
      return { label: host.firstElementChild.getAttribute('aria-label'), value: segmentedValue(host, 'Роль') };
    })).toEqual({ label: 'Role', value: 'admin' });
  });
});
