import { defineConfig } from '@playwright/test';

// Порты задаются окружением: параллельные прогоны в разных worktree не должны делить сервер (E2E_PORT, второй = +1).
const PORT = Number(process.env.E2E_PORT || 4173);
const PORT2 = PORT + 1;

export default defineConfig({
  testDir: './tests',
  fullyParallel: true,
  reporter: 'list',
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    browserName: 'chromium',
    serviceWorkers: 'block',
    trace: 'retain-on-failure',
  },
  projects: [
    {
      name: 'desktop-chromium',
      use: { viewport: { width: 1280, height: 800 } },
    },
    {
      name: 'mobile-chromium',
      use: { viewport: { width: 393, height: 852 }, deviceScaleFactor: 1, isMobile: true, hasTouch: true },
    },
  ],
  // Второй сервер с обычными заголовками кэширования (порт PORT+1): для офлайн-сценария со service worker.
  webServer: [
    {
      command: 'node static-server.mjs',
      env: { PORT: String(PORT) },
      url: `http://127.0.0.1:${PORT}`,
      reuseExistingServer: !process.env.CI,
      timeout: 10_000,
    },
    {
      command: 'node static-server.mjs',
      env: { PORT: String(PORT2), STATIC_CACHE: '1' },
      url: `http://127.0.0.1:${PORT2}`,
      reuseExistingServer: !process.env.CI,
      timeout: 10_000,
    },
  ],
});
