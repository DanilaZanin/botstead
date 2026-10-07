// ws.js: подключение к /api/ws?token&thread_id&since с переподключением и дочиткой хвоста.
import { MOCK, getToken, getEvents, authMe } from './api.js';

const RECONNECT_MIN = 1000;
const RECONNECT_MAX = 30000;

export function openThreadStream(threadId, sinceSeq, onEvent) {
  let closed = false;
  let since = sinceSeq || 0;
  let socket = null;
  let retryDelay = RECONNECT_MIN;
  let retryTimer = null;
  let mockTimer = null;

  async function tailRead() {
    try {
      const events = await getEvents(threadId, since);
      for (const ev of events) {
        since = Math.max(since, ev.seq);
        onEvent(ev);
      }
    } catch {
      // тред мог ещё не появиться: молча ждём следующего тика
    }
  }

  function connectMock() {
    tailRead();
    mockTimer = setInterval(tailRead, String(threadId).startsWith('g-') ? 300 : 1500);
  }

  function connectReal() {
    if (closed) return;
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const base = new URL('api/ws', location.href);
    // Cookie-сессия уходит с рукопожатием сама; query-токен только для устаревшего входа по OWNER_TOKEN.
    const token = getToken();
    const url = `${proto}://${base.host}${base.pathname}?${token ? `token=${encodeURIComponent(token)}&` : ''}thread_id=${encodeURIComponent(threadId)}&since=${since}`;
    socket = new WebSocket(url);
    let opened = false;
    socket.onopen = () => { opened = true; retryDelay = RECONNECT_MIN; };
    socket.onmessage = (msg) => {
      try {
        const ev = JSON.parse(msg.data);
        since = Math.max(since, ev.seq || 0);
        onEvent(ev);
      } catch { /* ignore malformed frame */ }
    };
    socket.onclose = (event) => {
      if (closed) return;
      // 4401 или отказ в рукопожатии: сессия могла закрыться. Проверяем её, при 401 приложение покажет вход.
      if (event.code === 4401 || !opened) {
        authMe().catch((err) => { if (err.status === 401) window.dispatchEvent(new CustomEvent('bothub-unauthorized')); });
      }
      retryTimer = setTimeout(connectReal, retryDelay);
      retryDelay = Math.min(retryDelay * 2, RECONNECT_MAX);
    };
    socket.onerror = () => { socket && socket.close(); };
  }

  // Дочитать хвост сразу при подключении, затем перейти на живой канал.
  tailRead().then(() => { if (!closed) { if (MOCK) connectMock(); else connectReal(); } });

  return function close() {
    closed = true;
    if (socket) socket.close();
    if (retryTimer) clearTimeout(retryTimer);
    if (mockTimer) clearInterval(mockTimer);
  };
}
