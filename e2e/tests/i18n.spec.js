import { expect, test } from '@playwright/test';

// Тесты рантайма локализации pwa/i18n.js (определение языка, сохранение, переводы, плейсхолдеры, плюрализация).

// setLang шлёт bothub:lang, а app.js на него перезагружает страницу. Тесты, которые переключают язык вызовом модуля,
// идут на документе без приложения: модуль i18n.js грузится динамическим import, как и в экране.
const NO_APP = '/manifest.webmanifest';

test.describe('i18n runtime', () => {
  test('определение языка по умолчанию для navigator.language ru-RU', async ({ page }) => {
    await page.addInitScript(() => {
      try { localStorage.removeItem('bothub.lang'); } catch {}
      Object.defineProperty(navigator, 'language', {
        get: () => 'ru-RU',
        configurable: true,
      });
    });
    await page.goto('/?mock=1');
    const lang = await page.evaluate(async () => {
      const { getLang } = await import('/i18n.js');
      return getLang();
    });
    expect(lang).toBe('ru');
  });

  test('без сохранённого выбора язык ru даже при navigator.language en-US (английский включает только переключатель)', async ({ page }) => {
    await page.addInitScript(() => {
      try { localStorage.removeItem('bothub.lang'); } catch {}
      Object.defineProperty(navigator, 'language', {
        get: () => 'en-US',
        configurable: true,
      });
    });
    await page.goto('/?mock=1');
    const lang = await page.evaluate(async () => {
      const { getLang } = await import('/i18n.js');
      return getLang();
    });
    expect(lang).toBe('ru');
  });

  test('setLang сохраняет значение, обновляет html lang и отправляет событие bothub:lang', async ({ page }) => {
    await page.goto(NO_APP);
    const res = await page.evaluate(async () => {
      const { setLang, getLang } = await import('/i18n.js');

      let firedDetail = null;
      const onLang = (ev) => { firedDetail = ev.detail; };
      window.addEventListener('bothub:lang', onLang);

      setLang('en');
      const enSaved = localStorage.getItem('bothub.lang');
      const enCurrent = getLang();
      const enHtmlLang = document.documentElement.lang;
      const enDetail = firedDetail;

      firedDetail = null;
      setLang('ru');
      const ruSaved = localStorage.getItem('bothub.lang');
      const ruCurrent = getLang();
      const ruHtmlLang = document.documentElement.lang;
      const ruDetail = firedDetail;

      window.removeEventListener('bothub:lang', onLang);

      return {
        enSaved,
        enCurrent,
        enHtmlLang,
        enDetail,
        ruSaved,
        ruCurrent,
        ruHtmlLang,
        ruDetail,
      };
    });

    expect(res.enSaved).toBe('en');
    expect(res.enCurrent).toBe('en');
    expect(res.enHtmlLang).toBe('en');
    expect(res.enDetail).toEqual({ lang: 'en' });

    expect(res.ruSaved).toBe('ru');
    expect(res.ruCurrent).toBe('ru');
    expect(res.ruHtmlLang).toBe('ru');
    expect(res.ruDetail).toEqual({ lang: 'ru' });
  });

  test('t возвращает английскую строку для ключей из каталогов и исходную строку для неизвестного ключа', async ({ page }) => {
    await page.goto(NO_APP);
    const res = await page.evaluate(async () => {
      const { setLang, loadLang, t } = await import('/i18n.js');
      setLang('en');
      await loadLang();

      return {
        showPassword: t('Показать пароль'),
        close: t('Закрыть'),
        providers: t('Провайдеры'),
        unknown: t('Несуществующий ключ в каталоге'),
      };
    });

    expect(res.showPassword).toBe('Show password');
    expect(res.close).toBe('Close');
    expect(res.providers).toBe('Providers');
    expect(res.unknown).toBe('Несуществующий ключ в каталоге');
  });

  test('t выполняет подстановку плейсхолдеров ${name} и сохраняет плейсхолдеры без значения', async ({ page }) => {
    await page.goto(NO_APP);
    const res = await page.evaluate(async () => {
      const { setLang, loadLang, t } = await import('/i18n.js');

      setLang('ru');
      const ruSub = t('Привет, ${name}! Новых задач: ${count}, статус: ${status}', {
        name: 'Денис',
        count: 7,
      });

      setLang('en');
      await loadLang();
      const enCatalogSub = t('Подключено моделей: ${ready}. Добавить ещё можно в разделе «Провайдеры».', {
        ready: 4,
      });
      const enUnmatched = t('User ${user} has role ${role}', {
        user: 'alice',
      });

      return { ruSub, enCatalogSub, enUnmatched };
    });

    expect(res.ruSub).toBe('Привет, Денис! Новых задач: 7, статус: ${status}');
    expect(res.enCatalogSub).toBe('Connected models: 4. Add more in "Providers" section.');
    expect(res.enUnmatched).toBe('User alice has role ${role}');
  });

  test('plural возвращает правильные формы для 1, 2, 5, 11, 21 в русском и 1, 2 в английском', async ({ page }) => {
    await page.goto(NO_APP);
    const res = await page.evaluate(async () => {
      const { setLang, plural } = await import('/i18n.js');
      const forms = {
        ru: ['бот', 'бота', 'ботов'],
        en: ['bot', 'bots'],
      };

      setLang('ru');
      const ru = {
        1: plural(1, forms),
        2: plural(2, forms),
        5: plural(5, forms),
        11: plural(11, forms),
        21: plural(21, forms),
      };

      setLang('en');
      const en = {
        1: plural(1, forms),
        2: plural(2, forms),
      };

      return { ru, en };
    });

    expect(res.ru[1]).toBe('бот');
    expect(res.ru[2]).toBe('бота');
    expect(res.ru[5]).toBe('ботов');
    expect(res.ru[11]).toBe('ботов');
    expect(res.ru[21]).toBe('бот');

    expect(res.en[1]).toBe('bot');
    expect(res.en[2]).toBe('bots');
  });

  test('HTML-строка в vars возвращается без экранирования и изменений как текст', async ({ page }) => {
    await page.goto('/?mock=1');
    const res = await page.evaluate(async () => {
      const { t } = await import('/i18n.js');
      const htmlPayload = '<b class="bold">жирный & "кавычки"</b><script>alert(1)</script>';
      const formatted = t('Шаблон: ${content}', { content: htmlPayload });
      return { formatted, htmlPayload };
    });

    expect(res.formatted).toBe(`Шаблон: ${res.htmlPayload}`);
  });

  test('formatNumber и formatDate используют текущий язык через Intl', async ({ page }) => {
    await page.goto(NO_APP);
    const res = await page.evaluate(async () => {
      const { setLang, formatNumber, formatDate } = await import('/i18n.js');
      const date = new Date('2026-10-05T12:00:00Z');

      setLang('ru');
      const ruNum = formatNumber(1000);
      const ruDate = formatDate(date, { month: 'short', day: 'numeric', timeZone: 'UTC' });

      setLang('en');
      const enNum = formatNumber(1000);
      const enDate = formatDate(date, { month: 'short', day: 'numeric', timeZone: 'UTC' });

      return { ruNum, ruDate, enNum, enDate };
    });

    expect(res.ruNum.replace(/\s+/g, ' ')).toBe('1 000');
    expect(res.enNum).toBe('1,000');
    expect(res.enDate).toContain('Oct');
  });
});
