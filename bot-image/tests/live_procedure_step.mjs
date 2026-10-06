// Живой тест procedure-step.mjs с настоящим Chromium (образец: проба оценщика live.mjs). Запускает test_procedure_step.py
// (LiveChromiumTests) при BOTHUB_LIVE_CHROMIUM=1 и BOTHUB_PLAYWRIGHT_CORE=<путь к playwright-core или к его index.js>;
// BOTHUB_CHROMIUM_EXECUTABLE (необязательно) задаёт бинарник Chromium, иначе берётся тот, что знает Playwright.
// Два локальных сервера (разные origin по порту) и шпионский скрипт на странице «чужого» origin считают, что до него дошло.
// Вывод: одна строка JSON {сценарий: результат}; проверяет их Python-тест.
import { createRequire } from 'node:module';
import http from 'node:http';
import net from 'node:net';
import { pathToFileURL } from 'node:url';
import path from 'node:path';

const require = createRequire(import.meta.url);
const core = process.env.BOTHUB_PLAYWRIGHT_CORE;
const pw = require(core.endsWith('.js') ? core : path.join(core, 'index.js'));
const here = path.dirname(new URL(import.meta.url).pathname);
const { runStep, parseInput, cssSelectorOk, urlSha256 } = await import(pathToFileURL(path.join(here, '..', 'procedure-step.mjs')).href);

const leaks = [];
const ports = {};
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const SPY = `<script>
document.addEventListener('input', (e) => fetch('/leak?v=' + encodeURIComponent(e.target.value)));
document.addEventListener('click', (e) => { if (e.target.tagName === 'BUTTON') fetch('/leak?click=1'); });
</script>`;

function serve(name, otherName) {
  return new Promise((resolve) => {
    const server = http.createServer((req, rsp) => {
      const url = new URL(req.url, 'http://x');
      if (url.pathname === '/leak') {
        leaks.push({ origin: name, v: url.searchParams.get('v'), click: url.searchParams.get('click') });
        rsp.end('ok');
        return;
      }
      rsp.setHeader('content-type', 'text/html');
      const other = `http://127.0.0.1:${ports[otherName]}`;
      if (url.pathname === '/login') rsp.end(`<label>Password <input id=pw type=text aria-label=Password></label><button>Pay</button>${SPY}`);
      else if (url.pathname === '/iframe') rsp.end(`<h1>bank</h1><iframe src="${other}/login"></iframe>`);
      else if (url.pathname === '/slow') {
        rsp.end(`<input id=pw type=text aria-label=Password disabled><button disabled>Pay</button>
          <script>setTimeout(() => { location = '${other}/login'; }, ${Number(url.searchParams.get('t')) || 400});</script>`);
      } else rsp.end('<p>hi</p>');
    });
    server.listen(0, '127.0.0.1', () => { ports[name] = server.address().port; resolve(server); });
  });
}

const freePort = () => new Promise((resolve) => {
  const probe = net.createServer().listen(0, '127.0.0.1', () => { const { port } = probe.address(); probe.close(() => resolve(port)); });
});

const servers = [await serve('A', 'B'), await serve('B', 'A')];
const cdpPort = await freePort();
const launchOptions = { headless: true, args: [`--remote-debugging-port=${cdpPort}`] };
if (process.env.BOTHUB_CHROMIUM_EXECUTABLE) launchOptions.executablePath = process.env.BOTHUB_CHROMIUM_EXECUTABLE;
const browser = await pw.chromium.launch(launchOptions);
const chromium = { connectOverCDP: (_url, options) => pw.chromium.connectOverCDP(`http://127.0.0.1:${cdpPort}`, options) };

const a = (p) => `http://127.0.0.1:${ports.A}${p}`;
const SECRET = 'S3CRET-live-xyz';
const step = (envelope) => runStep(parseInput(JSON.stringify(envelope)), { chromium });
const brief = (r) => ({ ok: r.ok, code: r.code, acted: r.acted, url: r.url, found: r.found });

async function fresh(urls) {
  const context = (await pw.chromium.connectOverCDP(`http://127.0.0.1:${cdpPort}`)).contexts()[0];
  for (const page of context.pages()) await page.close();
  for (const url of urls) { const page = await context.newPage(); await page.goto(url); }
}
const field = { role: 'textbox', name: 'Password' };
const expectedFor = (url, target = field) => ({ origin: a(''), url, role: target.role, name: target.name });
const out = {};

// J0 контроль: обычная страница, поле и кнопка доступны, действие идёт через handle
await fresh([a('/login')]);
out.j0_fill = brief(await step({ dry_run: false, deadline_ms: 8000, payload: { step: { action: 'fill', target: field, value: SECRET },
  expected: expectedFor(a('/login')), secret_input: true } }));
out.j0_click = brief(await step({ dry_run: false, deadline_ms: 8000, payload: { step: { action: 'click', target: { role: 'button', name: 'Pay' } },
  expected: expectedFor(a('/login'), { role: 'button', name: 'Pay' }) } }));
await sleep(300);
out.j0_leaks = leaks.splice(0).map((x) => ({ origin: x.origin, secret: x.v === SECRET, click: x.click }));

// J1 то же поле во фрейме другого origin: CSS и роль фрейм не пробивают
await fresh([a('/iframe')]);
await sleep(500);
out.j1 = [];
for (const target of [{ selector: '#pw' }, field]) {
  out.j1.push(brief(await step({ dry_run: true, deadline_ms: 5000, payload: { step: { action: 'fill', target } } })));
}

// J2 две вкладки с одним адресом: ambiguous, ничего не введено
await fresh([a('/login'), a('/login')]);
leaks.length = 0;
out.j2_action = brief(await step({ dry_run: false, deadline_ms: 5000, payload: { step: { action: 'fill', target: { selector: '#pw' }, value: SECRET },
  expected: expectedFor(a('/login')), secret_input: true } }));
out.j2_read = brief(await step({ dry_run: true, deadline_ms: 5000, payload: { step: { action: 'fill', target: field } } }));
await sleep(300);
out.j2_leaks = leaks.splice(0).length;

// J3 длинный адрес: чтение даёт обрезанный показ и хэш полного, действие находит вкладку по хэшу
const longUrl = a(`/login?t=${'x'.repeat(3000)}`);
await fresh([longUrl]);
const read = await step({ dry_run: true, deadline_ms: 5000, payload: { step: { action: 'click', target: { role: 'button', name: 'Pay' } } } });
out.j3_read = { code: read.code, url_len: read.url && read.url.length, sha_ok: read.url_sha256 === urlSha256(longUrl) };
const action3 = await step({ dry_run: false, deadline_ms: 5000, payload: { step: { action: 'click', target: { role: 'button', name: 'Pay' } },
  expected: { ...expectedFor(read.url, { role: 'button', name: 'Pay' }), url_sha256: read.url_sha256 } } });
out.j3_action = brief(action3);
out.j3_action.url = action3.url && action3.url.length;

// J4 поле disabled, через 800 мс страница уходит на другой origin: секрет не уходит, действие не выполняется
await fresh([a('/slow?t=800')]);
leaks.length = 0;
out.j4_read = brief(await step({ dry_run: true, deadline_ms: 5000, payload: { step: { action: 'fill', target: field } } }));
await fresh([a('/slow?t=800')]);
const started = Date.now();
out.j4 = brief(await step({ dry_run: false, deadline_ms: 8000, payload: { step: { action: 'fill', target: field, value: SECRET },
  expected: expectedFor(a('/slow?t=800')), secret_input: true } }));
out.j4.ms = Date.now() - started;
await sleep(1500);
out.j4_leaks = leaks.splice(0).map((x) => ({ origin: x.origin, secret: x.v === SECRET, click: x.click }));

// J4b то же с кликом по disabled-кнопке: на чужой странице кнопка Pay живая и шпионит
await fresh([a('/slow?t=800')]);
leaks.length = 0;
out.j4b = brief(await step({ dry_run: false, deadline_ms: 8000, payload: { step: { action: 'click', target: { role: 'button', name: 'Pay' } },
  expected: expectedFor(a('/slow?t=800'), { role: 'button', name: 'Pay' }) } }));
await sleep(1500);
out.j4b_leaks = leaks.splice(0).map((x) => ({ origin: x.origin, secret: x.v === SECRET, click: x.click }));

// J5 селекторы: обычный CSS проходит, псевдоклассы и движки нет
const selectors = ['#pw', 'a[href^="https://x"]', 'input[value=".."]', 'button:has-text("Pay")', ':text("Pay")', '#pw:visible', '*:light(#pw)',
  'a:hover', 'xpath=//a', ' text=Pay', '#a >> #b', 'internal:control=enter-frame'];
out.j5 = Object.fromEntries(selectors.map((s) => [s, cssSelectorOk(s)]));

await browser.close();
for (const server of servers) server.close();
process.stdout.write(`${JSON.stringify(out)}\n`);
process.exit(0);
