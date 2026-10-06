import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './tests',
  fullyParallel: true,
  reporter: 'list',
  use: {
    baseURL: 'http://127.0.0.1:4173',
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
  // Второй сервер с обычными заголовками кэширования (порт 4174): для офлайн-сценария со service worker.
  webServer: [
    {
      command: 'node static-server.mjs',
      url: 'http://127.0.0.1:4173',
      reuseExistingServer: !process.env.CI,
      timeout: 10_000,
    },
    {
      command: 'node static-server.mjs',
      env: { PORT: '4174', STATIC_CACHE: '1' },
      url: 'http://127.0.0.1:4174',
      reuseExistingServer: !process.env.CI,
      timeout: 10_000,
    },
  ],
});
