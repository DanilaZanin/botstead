// ui.js: общие хелперы разметки (иконки, экранирование, шапка) для app.js и account.js.
import { locale } from './i18n.js';

export function icon(inner, size = 20, sw = 2) {
  return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="${sw}" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${inner}</svg>`;
}

export const ICONS = {
  back: icon('<path d="M15 6l-6 6 6 6"></path>'),
  plus: icon('<path d="M12 5v14M5 12h14"></path>', 16, 2.4),
  bots: icon('<rect x="4" y="7" width="16" height="12" rx="3"></rect><path d="M12 3v4M9 13h.01M15 13h.01"></path>'),
  routines: icon('<circle cx="12" cy="12" r="9"></circle><path d="M12 7v5l3 2"></path>'),
  approvals: icon('<path d="M12 3l8 4v5c0 5-3.5 8-8 9-4.5-1-8-4-8-9V7z"></path><path d="M9 12l2 2 4-4"></path>'),
  usage: icon('<path d="M5 20V10M12 20V4M19 20v-7"></path>'),
  mic: icon('<rect x="9" y="3" width="6" height="11" rx="3"></rect><path d="M5 11a7 7 0 0014 0M12 18v3"></path>'),
  send: icon('<path d="M12 19V5M5 12l7-7 7 7"></path>'),
  screen: icon('<rect x="3" y="4" width="18" height="12" rx="2"></rect><path d="M8 20h8M12 16v4"></path>'),
  stop: icon('<rect x="7" y="7" width="10" height="10" rx="2"></rect>', 16, 2.4),
  alert: icon('<path d="M12 3l9 16H3z"></path><path d="M12 10v4M12 17h.01"></path>', 16, 2.2),
  check: icon('<path d="M5 12l5 5 9-10"></path>', 16, 2.6),
  search: icon('<circle cx="11" cy="11" r="7"></circle><path d="M20 20l-4-4"></path>', 18),
  play: icon('<path d="M8 5l11 7-11 7z"></path>'),
  lightning: icon('<path d="M13 2L4 14h7l-1 8 9-12h-7z"></path>'),
  folder: icon('<path d="M3 6h6l2 2h10v11H3z"></path>'),
  checklist: icon('<path d="M9 6h11M9 12h11M9 18h11M4 6h.01M4 12h.01M4 18h.01"></path>'),
  laptop: icon('<rect x="4" y="5" width="16" height="11" rx="2"></rect><path d="M2 19h20"></path>'),
  settings: icon('<path d="M12.2 2h-.4a2 2 0 00-2 2v.2a2 2 0 01-1 1.7l-.4.3a2 2 0 01-2 0l-.2-.1a2 2 0 00-2.7.7l-.2.4a2 2 0 00.7 2.7l.1.1a2 2 0 011 1.7v.5a2 2 0 01-1 1.7l-.1.1a2 2 0 00-.7 2.7l.2.4a2 2 0 002.7.7l.2-.1a2 2 0 012 0l.4.3a2 2 0 011 1.7v.2a2 2 0 002 2h.4a2 2 0 002-2v-.2a2 2 0 011-1.7l.4-.3a2 2 0 012 0l.2.1a2 2 0 002.7-.7l.2-.4a2 2 0 00-.7-2.7l-.1-.1a2 2 0 01-1-1.7v-.5a2 2 0 011-1.7l.1-.1a2 2 0 00.7-2.7l-.2-.4a2 2 0 00-2.7-.7l-.2.1a2 2 0 01-2 0l-.4-.3a2 2 0 01-1-1.7V4a2 2 0 00-2-2z"></path><circle cx="12" cy="12" r="3"></circle>'),
  chevronDown: icon('<path d="M6 9l6 6 6-6"></path>', 16, 2.6),
  chevronRight: icon('<path d="M9 6l6 6-6 6"></path>', 16, 2.2),
  download: icon('<path d="M12 4v11M7 10l5 5 5-5M5 20h14"></path>', 16, 2.4),
  preview: icon('<path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12z"></path><circle cx="12" cy="12" r="3"></circle>', 16, 2.4),
  memory: icon('<path d="M12 4a4 4 0 00-4 4 4 4 0 00-2 7 4 4 0 006 4 4 4 0 006-4 4 4 0 00-2-7 4 4 0 00-4-4z"></path><path d="M12 4v16"></path>', 18),
  handoff: icon('<path d="M8 13V5a1.5 1.5 0 013 0v6M11 11V4a1.5 1.5 0 013 0v7M14 11V5.5a1.5 1.5 0 013 0V14c0 4-2.5 7-6.5 7S5 18 4.5 15L3 11.5a1.5 1.5 0 012.6-1.4L8 13"></path>', 16, 2.4),
  spinner: icon('<circle cx="12" cy="12" r="8" stroke-dasharray="34 50"></circle>', 16, 2.4),
  close: icon('<path d="M6 6l12 12M18 6L6 18"></path>', 12, 2.4),
  closeLg: icon('<path d="M6 6l12 12M18 6L6 18"></path>', 20, 2.2),
  eye: icon('<path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12z"></path><circle cx="12" cy="12" r="3"></circle>'),
  eyeOff: icon('<path d="M3 3l18 18"></path><path d="M10.6 5.1A9.7 9.7 0 0112 5c6 0 10 7 10 7a17 17 0 01-3.2 4M6.5 6.5A17 17 0 002 12s4 7 10 7a9.7 9.7 0 004.2-1"></path><path d="M9.9 9.9a3 3 0 004.2 4.2"></path>'),
  user: icon('<circle cx="12" cy="8" r="4"></circle><path d="M4 20c0-4 4-6 8-6s8 2 8 6"></path>'),
  users: icon('<circle cx="9" cy="8" r="3.5"></circle><path d="M2.5 19c0-3.5 3-5.5 6.5-5.5s6.5 2 6.5 5.5M16 4.8a3.5 3.5 0 010 6.4M18 14c2.3.6 3.5 2.4 3.5 5"></path>'),
  lock: icon('<rect x="5" y="11" width="14" height="9" rx="2"></rect><path d="M8 11V8a4 4 0 018 0v3"></path>'),
  key: icon('<circle cx="8" cy="15" r="4"></circle><path d="M11 12l9-9M16 7l3 3"></path>'),
  link: icon('<path d="M10 14a4 4 0 005.7 0l3-3a4 4 0 00-5.7-5.7l-1 1M14 10a4 4 0 00-5.7 0l-3 3a4 4 0 005.7 5.7l1-1"></path>'),
  copy: icon('<rect x="8" y="8" width="12" height="12" rx="2"></rect><path d="M16 8V6a2 2 0 00-2-2H6a2 2 0 00-2 2v8a2 2 0 002 2h2"></path>', 16, 2.4),
  share: icon('<path d="M12 4v11M7 8l5-5 5 5M5 14v5h14v-5"></path>', 16, 2.4),
  sliders: icon('<path d="M4 7h10M18 7h2M4 17h2M10 17h10"></path><circle cx="16" cy="7" r="2"></circle><circle cx="8" cy="17" r="2"></circle>'),
  activity: icon('<path d="M3 12h4l3-8 4 16 3-8h4"></path>'),
  plug: icon('<path d="M9 3v5M15 3v5M6 8h12v3a6 6 0 01-12 0zM12 17v4"></path>'),
  device: icon('<rect x="7" y="3" width="10" height="18" rx="2.5"></rect><path d="M11 18h2"></path>'),
  tablet: icon('<rect x="4" y="3" width="16" height="18" rx="2.5"></rect><path d="M11 18h2"></path>'),
  browser: icon('<rect x="3" y="4" width="18" height="16" rx="2.5"></rect><path d="M3 9h18M7 6.5h.01M10 6.5h.01"></path>'),
  more: icon('<circle cx="5" cy="12" r="1.2"></circle><circle cx="12" cy="12" r="1.2"></circle><circle cx="19" cy="12" r="1.2"></circle>', 20, 2.6),
  logout: icon('<path d="M10 4H6a2 2 0 00-2 2v12a2 2 0 002 2h4M15 8l4 4-4 4M19 12H9"></path>'),
  retry: icon('<path d="M20 11a8 8 0 00-14.5-4M4 4v4h4"></path><path d="M4 13a8 8 0 0014.5 4M20 20v-4h-4"></path>', 16, 2.4),
  terminal: icon('<rect x="3" y="4" width="18" height="16" rx="2.5"></rect><path d="M7 10l3 2-3 2M12 15h5"></path>'),
  trash: icon('<path d="M4 7h16M10 11v6M14 11v6M6 7l1 12a2 2 0 002 2h6a2 2 0 002-2l1-12M9 7V4h6v3"></path>', 16, 2.2),
  external: icon('<path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 01-1 1H5a1 1 0 01-1-1V7a1 1 0 011-1h5"></path>', 16, 2.4),
  clipboard: icon('<rect x="6" y="5" width="12" height="16" rx="2"></rect><path d="M9 5V3h6v2M9 11h6M9 15h4"></path>', 16, 2.4),
  expand: icon('<path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"></path>', 20, 2.2),
  shrink: icon('<path d="M9 4v5H4M15 4v5h5M9 20v-5H4M15 20v-5h5"></path>', 20, 2.2),
  fit: icon('<rect x="3" y="5" width="18" height="14" rx="2"></rect><path d="M8 12h8M10 10l-2 2 2 2M14 10l2 2-2 2"></path>', 20, 2.2),
  actual: icon('<rect x="3" y="5" width="18" height="14" rx="2"></rect><path d="M9 10v4M9 10l-1.2.8M13 10v4M16 10v4"></path>', 20, 2.2),
  cloudOff: icon('<path d="M3 3l18 18"></path><path d="M7 7a5 5 0 00-1 9.9h10M10 5.2A6 6 0 0118 10a4 4 0 012.6 6.5"></path>'),
};

export function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// Плашка с ошибкой для форм: role=alert и tabindex=-1, чтобы после сбоя фокус можно было перевести на сообщение.
export function alertHtml(title, text = '', kind = 'danger') {
  return `<div class="banner banner-${kind}" role="alert" tabindex="-1"><span class="banner-icon">${ICONS.alert}</span><span class="banner-text"><span class="banner-title">${esc(title)}</span>${text ? `<span class="banner-sub">${esc(text)}</span>` : ''}</span></div>`;
}

export function backHeader({ title, subtitle, backHref, avatar, right }) {
  return `<header class="app-header">
    <a href="${backHref}" aria-label="Назад" class="icon-btn">${ICONS.back}</a>
    ${avatar || ''}
    <div class="flex-1 min-w-0 stack">
      <h1 class="t-headline header-title">${esc(title)}</h1>
      ${subtitle ? `<span class="t-footnote" style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${esc(subtitle)}</span>` : ''}
    </div>
    ${right || ''}
  </header>`;
}

// Дата и время без точки после месяца: «6 окт, 14:00».
export function fmtDateTime(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  const date = d.toLocaleDateString(locale(), { day: 'numeric', month: 'short' }).replace('.', '');
  const time = d.toLocaleTimeString(locale(), { hour: '2-digit', minute: '2-digit' });
  return `${date}, ${time}`;
}
export function fmtDate(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  return d.toLocaleDateString(locale(), { day: 'numeric', month: 'short' }).replace('.', '');
}

// Склонение по числу: plural(2, 'модель', 'модели', 'моделей') → «модели».
export function plural(n, one, few, many) {
  const m10 = Math.abs(n) % 10;
  const m100 = Math.abs(n) % 100;
  if (m10 === 1 && m100 !== 11) return one;
  if (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) return few;
  return many;
}

// «5 мин назад», «3 ч назад», «вчера» для отметок проверки.
export function fmtAgo(iso) {
  const t = new Date(iso).getTime();
  if (!iso || Number.isNaN(t)) return '';
  const min = Math.max(0, Math.round((Date.now() - t) / 60000));
  if (min < 1) return 'только что';
  if (min < 60) return `${min} мин назад`;
  const hr = Math.round(min / 60);
  if (hr < 24) return `${hr} ч назад`;
  return `${Math.round(hr / 24)} дн назад`;
}

// Читаемое имя модели. Ядро отдаёт display_name пустым, поэтому типовые идентификаторы переводим сами,
// остальное показываем как есть: подпись «claude-opus-5-5» под названием всё равно остаётся рядом.
export function modelTitle(name, displayName = '') {
  if (displayName) return displayName;
  const raw = String(name || '');
  let m = raw.match(/^claude-(opus|sonnet|haiku)-(\d+)(?:-(\d))?(?:-\d{8})?$/);
  if (m) return `${m[1][0].toUpperCase()}${m[1].slice(1)} ${m[2]}${m[3] ? `.${m[3]}` : ''}`;
  m = raw.match(/^gpt-([\w.]+?)(?:-(mini|nano|codex))?$/);
  if (m) return `GPT-${m[1]}${m[2] ? ` ${m[2]}` : ''}`;
  m = raw.match(/^gemini-([\d.]+)-(pro|flash-lite|flash)(?:-preview)?$/);
  if (m) return `Gemini ${m[1]} ${m[2][0].toUpperCase()}${m[2].slice(1).replace('-lite', ' Lite')}`;
  return raw;
}
