import { spawn } from 'node:child_process';
import { mkdir, rename, rm, writeFile } from 'node:fs/promises';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium, expect } from '@playwright/test';

const scriptDir = dirname(fileURLToPath(import.meta.url));
const e2eDir = resolve(scriptDir, '..');
const outputDir = resolve(e2eDir, 'demo-out');
const port = Number(process.env.DEMO_PORT || 4290);
const baseUrl = `http://127.0.0.1:${port}`;
const desktop = { width: 1280, height: 800 };
const mobile = { width: 390, height: 844 };

if (!Number.isInteger(port) || port < 1 || port > 65535) {
  throw new Error(`DEMO_PORT must be an integer from 1 to 65535. Received: ${process.env.DEMO_PORT}`);
}

const pause = (page, ms = 900) => page.waitForTimeout(ms);

const scenarios = [
  {
    name: 'create-from-catalog',
    viewport: desktop,
    async run(page) {
      await page.goto(`${baseUrl}/?mock=1&demo=1#/`);
      await expect(page.getByRole('heading', { name: 'Suggestions' })).toBeVisible();
      await pause(page, 900);

      // Desktop has no create button on this landing route, so use the app's New bot hash route.
      await page.evaluate(() => { location.hash = '#/bots/new'; });
      await expect(page.getByRole('heading', { name: 'New bot' })).toBeVisible();
      await pause(page, 900);

      await page.getByRole('button', { name: 'From catalog' }).click();
      const researcher = page.locator('[data-catalog-pick="researcher"]');
      await expect(researcher).toBeVisible();
      await pause(page, 1000);

      await researcher.click();
      const name = page.locator('#bn-name');
      await expect(name).toBeVisible();
      await pause(page, 900);
      await expect(name).toHaveValue('Researcher');
      await pause(page, 800);

      const create = page.locator('#bn-create');
      await expect(create).toBeEnabled({ timeout: 5000 });
      await create.click();
      await expect(page).toHaveURL(/#\/threads\/t-/);
      await expect(page.locator('#composer-input')).toBeVisible();
      await expect(page.locator('.desktop-thread-head')).toContainText('Researcher');
      await pause(page, 1800);
    },
  },
  {
    name: 'chat-and-approval',
    viewport: desktop,
    async run(page) {
      await page.goto(`${baseUrl}/?mock=1&demo=1#/threads/t-scout`);
      const input = page.locator('#composer-input');
      const approval = page.locator('[data-approval-id="ap1"]');
      await expect(input).toBeEnabled();
      await expect(approval).toBeVisible();
      await pause(page, 900);

      const message = 'Please summarize the latest research in three points.';
      await input.fill(message);
      await pause(page, 800);
      await page.locator('[data-action="send-message"]').click();
      await expect(page.locator('#thread-body')).toContainText(message);
      await expect(page.locator('#thread-body .msg-bot').filter({ hasText: 'The main finding is' })).toBeVisible();
      await pause(page, 1800);

      // Approval ap1 is seeded by the mock. Sending this message does not create it.
      await approval.scrollIntoViewIfNeeded();
      await pause(page, 900);
      await approval.getByRole('button', { name: 'Approve', exact: true }).click();
      await expect(approval.locator('[data-approval-status]')).toContainText('Approved');
      await pause(page, 1800);
    },
  },
  {
    name: 'group-debate',
    viewport: desktop,
    async run(page) {
      await page.goto(`${baseUrl}/?mock=1&demo=1#/`);
      await expect(page.locator('.desktop-sidebar')).toBeVisible();
      await pause(page, 900);

      await page.locator('.desktop-sidebar').getByRole('link', { name: 'Discussions', exact: true }).click();
      await expect(page.getByRole('heading', { name: 'Discussions' })).toBeVisible();
      await pause(page, 900);
      await page.getByRole('link', { name: 'New discussion', exact: true }).click();
      await expect(page.getByRole('heading', { name: 'New discussion' })).toBeVisible();
      await pause(page, 900);

      await page.locator('#gr-title').fill('Postgres or SQLite?');
      await page.locator('[data-bot-id="scout"]').click();
      await page.locator('[data-bot-id="coder"]').click();
      await page.locator('[data-mode="debate"]').click();
      await page.locator('#gr-rounds').fill('2');
      await pause(page, 1000);

      await page.locator('#gr-submit').click();
      await expect(page).toHaveURL(/#\/groups\/g-\d+$/);
      await expect(page.getByRole('heading', { name: 'Postgres or SQLite?' })).toBeVisible();
      const input = page.locator('#group-input');
      await expect(input).toBeEnabled();
      await pause(page, 900);

      const topic = 'Postgres or SQLite for a single-server bot hub?';
      await input.fill(topic);
      await pause(page, 800);
      await page.getByRole('button', { name: 'Send', exact: true }).click();
      await expect(page.locator('.msg-user')).toContainText(topic);
      const replies = page.locator('#thread-body .msg-group');
      await expect(replies.nth(0)).toBeVisible({ timeout: 10000 });
      await pause(page, 1600);
      await expect(replies).toHaveCount(4, { timeout: 15000 });
      await pause(page, 1800);
    },
  },
  {
    name: 'browser-takeover',
    viewport: desktop,
    async run(page) {
      await page.goto(`${baseUrl}/?mock=1&demo=1&browser=hold#/bots/sre/browser`);
      await expect(page.locator('#br')).toHaveAttribute('data-screen', 'live', { timeout: 10000 });
      const steps = page.locator('[data-steps-list][data-limit="200"] .br-step');
      await expect(steps.first()).toBeVisible();
      await expect(steps).toHaveCount(5);
      await pause(page, 900);
      await steps.first().scrollIntoViewIfNeeded();
      await pause(page, 900);

      const title = page.locator('#br-control-title');
      const before = await title.innerText();
      await page.getByRole('button', { name: 'Take control', exact: true }).click();
      const dialog = page.getByRole('dialog', { name: 'Take control?' });
      await expect(dialog).toBeVisible();
      await pause(page, 900);
      await dialog.getByRole('button', { name: 'Take control', exact: true }).click();
      await expect(dialog).toHaveCount(0);
      await expect(title).not.toHaveText(before);
      await pause(page, 1800);
    },
  },
  {
    name: 'activity-and-usage',
    viewport: desktop,
    async run(page) {
      await page.goto(`${baseUrl}/?mock=1&demo=1#/`);
      await expect(page.locator('.desktop-sidebar')).toBeVisible();
      await pause(page, 900);

      const nav = page.locator('.desktop-sidebar');
      await nav.getByRole('link', { name: 'Activity', exact: true }).click();
      await expect(page.getByRole('heading', { name: 'Activity', level: 1 })).toBeVisible();
      const activity = page.locator('.act-row');
      await expect(activity.first()).toBeVisible();
      await pause(page, 1800);
      await activity.first().scrollIntoViewIfNeeded();
      await pause(page, 900);
      await pause(page, 900);

      await nav.getByRole('link', { name: 'Usage', exact: true }).click();
      await expect(page.getByRole('heading', { name: 'Usage', level: 1 })).toBeVisible();
      const cost = page.locator('.usage-chart-card');
      await expect(cost).toBeVisible();
      await expect(cost).toContainText('$');
      await pause(page, 1800);
      await pause(page, 900);
    },
  },
  {
    name: 'mobile',
    viewport: mobile,
    isMobile: true,
    async run(page) {
      await page.goto(`${baseUrl}/?mock=1&demo=1#/`);
      await expect(page.getByRole('heading', { name: 'Bots', level: 1 })).toBeVisible();
      const scout = page.locator('[data-action="open-thread"][data-bot="scout"]').first();
      await expect(scout).toBeVisible();
      await pause(page, 900);

      await scout.click();
      await expect(page).toHaveURL(/#\/threads\/t-scout$/);
      await expect(page.locator('#composer-input')).toBeVisible();
      await pause(page, 1800);
      await pause(page, 900);

      await page.locator('.screen a[href="#/"][aria-label]').first().click();
      await expect(page.getByRole('heading', { name: 'Bots', level: 1 })).toBeVisible();
      const suggestions = page.getByRole('heading', { name: 'Suggestions', level: 2 });
      await expect(suggestions).toBeVisible();
      await suggestions.scrollIntoViewIfNeeded();
      await pause(page, 900);
      await expect(page.locator('[data-action="accept-suggestion"]')).toHaveCount(2);
      await pause(page, 1800);
      await pause(page, 900);
    },
  },
];

async function waitForServer(server) {
  const deadline = Date.now() + 15000;
  let lastError;
  while (Date.now() < deadline) {
    if (server.exitCode !== null) {
      throw new Error(`Static server exited with code ${server.exitCode}`);
    }
    try {
      const response = await fetch(`${baseUrl}/?mock=1&demo=1`);
      if (response.ok) return;
      lastError = new Error(`Static server returned HTTP ${response.status}`);
    } catch (error) {
      lastError = error;
    }
    await new Promise((resolvePromise) => setTimeout(resolvePromise, 150));
  }
  throw new Error(`Static server did not become ready: ${lastError?.message || 'timeout'}`);
}

async function stopServer(server) {
  if (server.exitCode !== null || server.signalCode !== null) return;
  server.kill('SIGTERM');
  await new Promise((done) => {
    const timer = setTimeout(() => {
      server.kill('SIGKILL');
      done();
    }, 2000);
    server.once('exit', () => {
      clearTimeout(timer);
      done();
    });
  });
}

async function record(browser, scenario) {
  let context;
  let page;
  let video;
  let failure;
  try {
    context = await browser.newContext({
      viewport: scenario.viewport,
      deviceScaleFactor: 1,
      colorScheme: 'light',
      isMobile: scenario.isMobile || false,
      hasTouch: scenario.isMobile || false,
      serviceWorkers: 'block',
      recordVideo: { dir: outputDir, size: scenario.viewport },
    });
    page = await context.newPage();
    await context.addInitScript(() => {
      localStorage.setItem('bothub.lang', 'en');
    });
    video = page.video();
    const startedAt = Date.now();
    await scenario.run(page);
    while (Date.now() - startedAt < 8200) {
      const remaining = 8200 - (Date.now() - startedAt);
      await pause(page, Math.max(600, Math.min(1200, remaining)));
    }
  } catch (error) {
    failure = error;
    const basename = resolve(outputDir, `${scenario.name}.fail`);
    try {
      const body = page ? await page.locator('body').innerText({ timeout: 3000 }) : '';
      await writeFile(`${basename}.txt`, `URL: ${page?.url() || 'page not created'}\n\n${body.slice(0, 3000)}\n${error.stack || error}\n`);
    } catch (diagnosticError) {
      await writeFile(`${basename}.txt`, `URL: ${page?.url() || 'page not created'}\n\nUnable to read body: ${diagnosticError.message}\n${error.stack || error}\n`);
    }
    try {
      if (page) await page.screenshot({ path: `${basename}.png`, fullPage: true });
    } catch (diagnosticError) {
      process.stderr.write(`Screenshot failed for ${scenario.name}: ${diagnosticError.message}\n`);
    }
  } finally {
    if (context) await context.close();
  }

  if (failure) {
    if (video) await rm(await video.path(), { force: true });
    throw failure;
  }
  const tempPath = await video.path();
  const target = resolve(outputDir, `${scenario.name}.webm`);
  await rm(target, { force: true });
  await rename(tempPath, target);
  await rm(resolve(outputDir, `${scenario.name}.fail.png`), { force: true });
  await rm(resolve(outputDir, `${scenario.name}.fail.txt`), { force: true });
  process.stdout.write(`Recorded ${target}\n`);
}

async function main() {
  await mkdir(outputDir, { recursive: true });
  const server = spawn(process.execPath, ['static-server.mjs'], {
    cwd: e2eDir,
    env: { ...process.env, PORT: String(port), STATIC_CACHE: '0' },
    stdio: 'inherit',
  });
  let browser;
  try {
    await waitForServer(server);
    browser = await chromium.launch();
    const results = [];
    for (const scenario of scenarios) {
      try {
        await record(browser, scenario);
        results.push({ name: scenario.name, status: 'ok' });
      } catch (error) {
        results.push({ name: scenario.name, status: 'fail' });
        process.stderr.write(`${scenario.name}: ${error.stack || error}\n`);
      }
    }
    process.stdout.write(`\nDemo recording summary:\n${results.map((result) => `  ${result.name}: ${result.status}`).join('\n')}\n`);
    if (results.some((result) => result.status === 'fail')) process.exitCode = 1;
  } finally {
    try {
      if (browser) await browser.close();
    } finally {
      await stopServer(server);
    }
  }
}

await main();
