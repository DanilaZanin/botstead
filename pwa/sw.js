// sw.js: кэш оболочки приложения + push-уведомления (approval_req → #/approvals/<id>).
// Данные пользователя не кэшируются: /api/* (включая /api/auth/*) и WebSocket идут мимо кэша, в кэше только статика.
// Версию бампать при каждом релизе оболочки, иначе обновления не доходят до телефона.
const CACHE = 'bothub-shell-v22';
const SHELL = [
  './',
  './index.html',
  './styles.css',
  './app.js',
  './api.js',
  './ws.js',
  './avatars.js',
  './ui.js',
  './account.js',
  './registry.js',
  './providers.js',
  './procedures.js',
  './memory.js',
  './activity.js',
  './wakeups.js',
  './provider-requests.js',
  './cli-login.js',
  './terminal.js',
  './browser.js',
  './screen.js',
  './i18n.js',
  './i18n-dom.js',
  './i18n/en-app.js',
  './i18n/en-features.js',
  './i18n/en-extra.js',
  './manifest.webmanifest',
  './icons/icon.svg',
  './avatars/scout.webp',
  './avatars/mac.webp',
  './avatars/sre.webp',
  './avatars/coder.webp',
  './avatars/archive.webp',
  './avatars/owl.webp',
  './avatars/spark.webp',
  './avatars/robot.webp',
  './avatars/fox.webp',
  './avatars/cat.webp',
];

// xterm.js для терминала входа (vendor/xterm/, MIT): файлы ставит vendor/xterm/install.sh. Если их ещё нет, оболочка
// всё равно кэшируется: эти файлы добавляются по одному и без них терминал работает во встроенном режиме.
const OPTIONAL_SHELL = [
  './vendor/xterm/xterm.js',
  './vendor/xterm/xterm.css',
  './vendor/xterm/addon-fit.js',
];

// noVNC для экрана браузера бота (vendor/novnc/, ядро пакета как ES-модули): модулей несколько десятков, поэтому их список
// лежит в vendor/novnc/precache.txt (по строке на файл, пишет install.sh). Нет списка или файла: оболочка всё равно кэшируется,
// а модули попадают в кэш при первом открытии экрана.
function optionalNovnc(cache) {
  return fetch('./vendor/novnc/precache.txt')
    .then((res) => (res.ok ? res.text() : ''))
    .then((text) => Promise.all(text.split('\n').map((line) => line.trim()).filter(Boolean).map((url) => cache.add(`./vendor/novnc/${url}`).catch(() => undefined))))
    .catch(() => undefined);
}

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE)
      .then((c) => c.addAll(SHELL).then(() => Promise.all([...OPTIONAL_SHELL.map((url) => c.add(url).catch(() => undefined)), optionalNovnc(c)])))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

// Офлайн: страница открывается из кэша оболочки. Адрес приложения бывает с query (start_url ?utm_source=pwa),
// поэтому для навигации query не сравниваем, а запасной вариант: index.html.
function offlineShell(req) {
  const navigate = req.mode === 'navigate';
  return caches.match(req, { ignoreSearch: navigate }).then((hit) => hit || (navigate ? caches.match('./index.html') : undefined));
}

// В кэш идут только успешные ответы своего origin без запрета кэширования.
function cacheable(res) {
  const control = res.headers.get('cache-control') || '';
  return res.ok && res.type === 'basic' && !/no-store|private/i.test(control) && !res.headers.has('set-cookie');
}

// Выход и смена пользователя: приложение просит удалить всё, кроме оболочки.
self.addEventListener('message', (event) => {
  if (!event.data || event.data.type !== 'clear-data') return;
  event.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))));
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.pathname.includes('/api/') || url.pathname.includes('/agent/')) return; // API, /api/auth/* и WS всегда живые
  if (req.headers.has('authorization')) return; // запросы с токеном не кэшируем

  // index.html и *.js: сеть первым делом, чтобы обновления доходили до телефона;
  // офлайн или сбой сети — отдаём то, что закэшировано.
  const networkFirst = req.mode === 'navigate' || url.pathname.endsWith('.js') || url.pathname.endsWith('.html') || url.pathname === '/';
  if (networkFirst) {
    event.respondWith(
      fetch(req).then((res) => {
        if (cacheable(res)) { const copy = res.clone(); caches.open(CACHE).then((c) => c.put(req, copy)); }
        return res;
      }).catch(() => offlineShell(req))
    );
    return;
  }

  event.respondWith(
    caches.match(req).then((cached) => cached || fetch(req).then((res) => {
      if (cacheable(res)) { const copy = res.clone(); caches.open(CACHE).then((c) => c.put(req, copy)); }
      return res;
    }).catch(() => cached))
  );
});

self.addEventListener('push', (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch { data = { title: 'botstead', body: event.data ? event.data.text() : '' }; }
  const title = data.title || 'botstead';
  const body = data.body || 'Нужно решение';
  const approvalId = data.approval_id || (data.payload && data.payload.approval_id);
  const url = approvalId ? `./#/approvals/${approvalId}` : './#/approvals';
  event.waitUntil(
    self.registration.showNotification(title, {
      body,
      icon: './icons/icon.svg',
      badge: './icons/icon.svg',
      data: { url },
      tag: approvalId || 'bothub',
    })
  );
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || './#/approvals';
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((list) => {
      for (const client of list) {
        if ('focus' in client) {
          client.navigate ? client.navigate(url) : null;
          client.postMessage({ type: 'navigate', url });
          return client.focus();
        }
      }
      return self.clients.openWindow(url);
    })
  );
});
