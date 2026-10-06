// pwa/i18n.js: рантайм переводов интерфейса PWA (vanilla JS, ES-модули, без сборки).

export const LANGS = [
  { code: 'ru', label: 'Русский' },
  { code: 'en', label: 'English' },
];

const STORAGE_KEY = 'bothub.lang';
const VALID_LANGS = new Set(LANGS.map((l) => l.code));

export function getLang() {
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved && VALID_LANGS.has(saved)) {
      return saved;
    }
  } catch {
    // localStorage may be unavailable or throw in restricted contexts
  }
  // Без сохранённого выбора всегда русский: язык браузера не учитывается, английский включает только переключатель.
  return 'ru';
}

export function setLang(code) {
  if (!VALID_LANGS.has(code)) {
    throw new Error(`Unsupported language code: ${code}`);
  }
  try {
    localStorage.setItem(STORAGE_KEY, code);
  } catch {
    // ignore storage write errors
  }
  if (typeof document !== 'undefined' && document.documentElement) {
    document.documentElement.lang = code;
  }
  if (code === 'ru') {
    currentCatalog = new Map();
  } else if (code === 'en' && enCatalog) {
    currentCatalog = enCatalog;
  }
  if (typeof window !== 'undefined') {
    window.dispatchEvent(new CustomEvent('bothub:lang', { detail: { lang: code } }));
  }
}

let currentCatalog = new Map();
let enCatalog = null;
let enPromise = null;

export async function loadLang(targetLang) {
  const lang = targetLang || getLang();
  if (lang === 'ru') {
    currentCatalog = new Map();
    return currentCatalog;
  }
  if (lang === 'en') {
    if (enCatalog) {
      currentCatalog = enCatalog;
      return currentCatalog;
    }
    if (!enPromise) {
      enPromise = Promise.all([
        import('./i18n/en-app.js'),
        import('./i18n/en-features.js'),
        import('./i18n/en-extra.js'),
      ]).then((mods) => {
        // Каталоги сливаются по порядку: более поздний дополняет, но не затирает уже известный перевод.
        const map = new Map();
        for (const mod of mods) {
          const obj = (mod && mod.default) || mod || {};
          for (const [k, v] of Object.entries(obj)) {
            if (!map.has(k)) map.set(k, v);
          }
        }
        enCatalog = map;
        return map;
      }).catch((err) => {
        enPromise = null;
        console.warn('Failed to load English translations, falling back to Russian:', err);
        return new Map();
      });
    }
    currentCatalog = await enPromise;
    return currentCatalog;
  }
  currentCatalog = new Map();
  return currentCatalog;
}

export function t(source, vars) {
  if (typeof source !== 'string') return source;
  const lang = getLang();
  let text = source;
  if (lang === 'en' && currentCatalog && currentCatalog.has(source)) {
    text = currentCatalog.get(source);
  }
  if (!vars || typeof vars !== 'object') {
    return text;
  }
  return text.replace(/\$\{([^}]+)\}/g, (match, key) => {
    const val = vars[key] !== undefined ? vars[key] : vars[key.trim()];
    if (val !== undefined && val !== null) {
      return String(val);
    }
    return match;
  });
}

function getPluralIndexRu(n) {
  const num = Math.abs(Math.floor(n));
  const rem10 = num % 10;
  const rem100 = num % 100;
  if (rem10 === 1 && rem100 !== 11) {
    return 0; // one
  }
  if (rem10 >= 2 && rem10 <= 4 && (rem100 < 12 || rem100 > 14)) {
    return 1; // few
  }
  return 2; // many
}

function getPluralIndexEn(n) {
  return Math.abs(n) === 1 ? 0 : 1;
}

export function plural(n, forms) {
  if (!forms || typeof forms !== 'object') return '';
  const lang = getLang();
  const branch = forms[lang] || forms.ru || forms.en;
  if (!branch) return '';
  if (Array.isArray(branch)) {
    const idx = lang === 'ru' ? getPluralIndexRu(n) : getPluralIndexEn(n);
    return branch[idx] ?? branch[branch.length - 1] ?? '';
  }
  if (typeof branch === 'object') {
    if (lang === 'ru') {
      const idx = getPluralIndexRu(n);
      const keys = ['one', 'few', 'many'];
      return branch[keys[idx]] ?? branch.many ?? branch.other ?? '';
    }
    const idx = getPluralIndexEn(n);
    return idx === 0 ? (branch.one ?? '') : (branch.other ?? branch.many ?? '');
  }
  return '';
}

export function formatNumber(n, options) {
  const lang = getLang();
  return new Intl.NumberFormat(lang, options).format(n);
}

export function formatDate(date, options) {
  const lang = getLang();
  const d = date instanceof Date ? date : new Date(date);
  return new Intl.DateTimeFormat(lang, options).format(d);
}

// Локаль для toLocale*String и Intl в экранах (даты и время).
export function locale() {
  return getLang() === 'en' ? 'en-US' : 'ru-RU';
}

// Текущий загруженный каталог (Map: русский ключ → английское значение); для ru пустой.
export function getCatalog() {
  return currentCatalog;
}
