// screen.js: клиент живого экрана бота. RFB поверх WebSocket (docs/contracts.md §13) читает noVNC: ядро пакета
// @novnc/novnc лежит в vendor/novnc/ (ставит vendor/novnc/install.sh) и подключается лениво, только на экране браузера бота.
// Сокет создаём сами и отдаём noVNC готовый канал: так видны коды закрытия 4401/4404/4409/4410/1013, которых у события
// disconnect нет. В мок-режиме вместо noVNC рисуется поддельный экран на canvas, ввод считается в window.__screenMock.
// Содержимое экрана и вводимые символы нигде не сохраняются: ни в хранилище страницы, ни в консоль.
import * as api from './api.js';

const NOVNC = './vendor/novnc/core/rfb.js';

let rfbClass = null;
function loadRfb() {
  if (!rfbClass) {
    rfbClass = import(NOVNC).then((m) => {
      if (typeof m.default !== 'function') throw new Error('novnc');
      return m.default;
    }).catch((err) => { rfbClass = null; throw err; });
  }
  return rfbClass;
}

// Клавиша для sendKey: символ или «Backspace»/«Enter». Keysym 0 значит «не передавать» (управляющие и составные знаки).
export function keysymOf(key) {
  if (key === 'Backspace') return 0xff08;
  if (key === 'Enter') return 0xff0d;
  const chars = Array.from(String(key ?? ''));
  if (chars.length !== 1) return 0;
  const cp = chars[0].codePointAt(0);
  if (cp < 0x20 || (cp >= 0x7f && cp < 0xa0)) return 0;
  return cp < 0x100 ? cp : 0x01000000 + cp;
}

// Контроллер экрана: одинаковый для noVNC и мока. onConnect() после рукопожатия RFB, onClose({code, opened}) один раз.
// Ошибка загрузки клиента (нет файлов vendor/novnc) выбрасывается из openScreen: экран покажет состояние «клиент не загрузился».
export async function openScreen({ host, botId, viewOnly, fit, onConnect, onClose }) {
  return api.MOCK ? openMock({ host, botId, viewOnly, fit, onConnect, onClose }) : openRfb({ host, botId, viewOnly, fit, onConnect, onClose });
}

async function openRfb({ host, botId, viewOnly, fit, onConnect, onClose }) {
  const RFB = await loadRfb();
  const ws = api.openScreenSocket(botId);
  let opened = false;
  let finished = false;
  let rfb = null;
  let last = null;
  const finish = (info) => { if (!finished) { finished = true; onClose(info); } };
  // Слушатель закрытия ставим до создания RFB: он срабатывает раньше разбора noVNC, пока холст ещё не снят, и успевает снять кадр.
  ws.addEventListener('open', () => { opened = true; });
  ws.addEventListener('close', (e) => {
    try { last = rfb && rfb.toDataURL(); } catch { last = null; }
    finish({ code: e.code, opened });
  });
  rfb = new RFB(host, ws, { shared: true });
  rfb.viewOnly = viewOnly;
  rfb.scaleViewport = fit;
  rfb.clipViewport = !fit;
  rfb.focusOnClick = !viewOnly;
  rfb.resizeSession = false;
  rfb.showDotCursor = true;
  rfb.background = '#0b0c0e';
  rfb.addEventListener('connect', () => {
    const canvas = host.querySelector('canvas');
    if (canvas) { canvas.tabIndex = -1; canvas.setAttribute('aria-hidden', 'true'); }
    onConnect();
  });
  rfb.addEventListener('credentialsrequired', () => { try { rfb.disconnect(); } catch { /* уже закрыт */ } finish({ code: 1011, opened }); });
  rfb.addEventListener('disconnect', () => finish({ code: 1006, opened }));
  return {
    setViewOnly(v) { rfb.viewOnly = v; rfb.focusOnClick = !v; if (v) rfb.blur(); },
    setFit(v) { rfb.scaleViewport = v; rfb.clipViewport = !v; },
    focus() { rfb.focus(); },
    blur() { rfb.blur(); },
    // Ввод с экранной клавиатуры телефона: нажатие и отпускание одной клавиши, без сохранения символа.
    sendKey(key) {
      const sym = keysymOf(key);
      if (sym) rfb.sendKey(sym, key === 'Backspace' || key === 'Enter' ? key : null);
    },
    get canvas() { return host.querySelector('canvas'); },
    lastFrame() { return last; },
    close() { finished = true; try { rfb.disconnect(); } catch { /* уже закрыт */ } try { ws.close(); } catch { /* уже закрыт */ } },
  };
}

// ---------------------------------------------------------------------------
// Мок: поддельный экран страницы входа на canvas
// ---------------------------------------------------------------------------
const W = 1280;
const H = 800;
const DEMO = api.MOCK && new URLSearchParams(location.search).get('demo') === '1';

function drawPage(ctx, { caret, dots, hint }) {
  ctx.fillStyle = '#eef1f4';
  ctx.fillRect(0, 0, W, H);
  ctx.fillStyle = '#16181a';
  ctx.fillRect(0, 0, W, 64);
  ctx.fillStyle = '#ffffff';
  ctx.font = '600 24px sans-serif';
  ctx.fillText(DEMO ? 'reports.example.com' : 'status.example.org', 32, 40);
  ctx.fillStyle = '#ffffff';
  ctx.fillRect(360, 160, 560, 440);
  ctx.strokeStyle = '#c9ced4';
  ctx.strokeRect(360, 160, 560, 440);
  ctx.fillStyle = '#16181a';
  ctx.font = '600 32px sans-serif';
  ctx.fillText(DEMO ? 'Sign in to reports' : 'Вход в панель', 400, 220);
  ctx.font = '400 20px sans-serif';
  ctx.fillStyle = '#565c62';
  ctx.fillText(DEMO ? 'Email' : 'Логин', 400, 280);
  ctx.fillText(DEMO ? 'Password' : 'Пароль', 400, 380);
  ctx.strokeStyle = '#858b83';
  ctx.strokeRect(400, 296, 480, 52);
  ctx.strokeRect(400, 396, 480, 52);
  ctx.fillStyle = '#16181a';
  ctx.fillText('operator', 416, 330);
  ctx.fillText('●'.repeat(dots), 416, 430);
  if (caret) ctx.fillRect(416 + dots * 14, 408, 2, 28);
  ctx.fillStyle = '#2b55d6';
  ctx.fillRect(400, 490, 480, 56);
  ctx.fillStyle = '#ffffff';
  ctx.font = '600 22px sans-serif';
  ctx.fillText(DEMO ? 'Sign in' : 'Войти', 604, 526);
  if (hint) {
    ctx.fillStyle = '#7a3e06';
    ctx.font = '400 18px sans-serif';
    ctx.fillText(hint, 400, 580);
  }
}

async function openMock({ host, botId, viewOnly, fit, onConnect, onClose }) {
  const ws = api.openScreenSocket(botId);
  const m = window.__screenMock;
  const canvas = document.createElement('canvas');
  canvas.width = W;
  canvas.height = H;
  canvas.tabIndex = -1;
  canvas.className = 'br-mock-canvas';
  canvas.setAttribute('aria-hidden', 'true');
  host.appendChild(canvas);
  const ctx = canvas.getContext('2d');
  const view = { viewOnly, fit, caret: true, dots: 0 };
  let timer = null;
  let finished = false;
  const paint = () => drawPage(ctx, view);
  const sync = () => { m.viewOnly = view.viewOnly; m.fit = view.fit; };
  const applyFit = () => { canvas.classList.toggle('is-actual', !view.fit); };
  paint();
  sync();
  applyFit();
  canvas.addEventListener('keydown', (e) => {
    if (view.viewOnly) return;
    e.preventDefault();
    m.keys += 1;
    (m.keyLog = m.keyLog || []).push(e.key.length === 1 ? 'char' : e.key); // имена служебных клавиш, символы не пишем
    if (e.key.length === 1) view.dots = Math.min(24, view.dots + 1);
    paint();
  });
  canvas.addEventListener('pointerdown', () => {
    if (view.viewOnly) return;
    m.clicks += 1;
    canvas.focus();
  });
  ws.addEventListener('open', () => {
    m.live = true;
    timer = setInterval(() => { view.caret = !view.caret; paint(); }, 650);
    onConnect();
  });
  ws.addEventListener('close', (e) => {
    m.live = false;
    clearInterval(timer);
    if (!finished) { finished = true; onClose({ code: e.code, opened: e.wasOpen }); }
  });
  return {
    setViewOnly(v) { view.viewOnly = v; sync(); if (v && document.activeElement === canvas) canvas.blur(); },
    setFit(v) { view.fit = v; sync(); applyFit(); },
    focus() { canvas.focus(); },
    blur() { canvas.blur(); },
    sendKey(key) {
      if (view.viewOnly || !keysymOf(key)) return;
      m.keys += 1;
      (m.keyLog = m.keyLog || []).push(key === 'Backspace' || key === 'Enter' ? key : 'char'); // как и выше: символы не пишем
      if (key === 'Backspace') view.dots = Math.max(0, view.dots - 1);
      else if (key !== 'Enter') view.dots = Math.min(24, view.dots + 1);
      paint();
    },
    get canvas() { return canvas; },
    lastFrame() { try { return canvas.toDataURL(); } catch { return null; } },
    close() { finished = true; clearInterval(timer); m.live = false; ws.close(); },
  };
}
