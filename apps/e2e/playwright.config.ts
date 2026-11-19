import { defineConfig, devices } from '@playwright/test'

// The suite runs the real web app (production build, `vite preview`) against the real API,
// PostgreSQL and inline jobs. Only AWS is replaced, by apps/e2e/server/fake_aws.py; see
// apps/e2e/README.md. Both servers start here unless already running (locally).
// The app is built and served under /app/, as on staging (docs/14 §7.2, SITE-13).

const API_URL = process.env.E2E_API_URL ?? 'http://127.0.0.1:8000'
const WEB_URL = process.env.E2E_WEB_URL ?? 'http://127.0.0.1:4173'
const APP_BASE = process.env.E2E_APP_BASE ?? '/app/'
const WEB_PORT = new URL(WEB_URL).port || '4173'
const CI = Boolean(process.env.CI)
const API_PORT = new URL(API_URL).port || '80'
// The servers to start. Set E2E_API_URL / E2E_WEB_URL (and CLOUDOPTICS_E2E_DATABASE, a name
// ending in _e2e) to run a second stack beside one already on :8000 and :4173.
const WEB_ORIGINS = [WEB_URL, WEB_URL.replace('127.0.0.1', 'localhost')]

// WebKit (Safari's engine) runs the specs most exposed to engine differences: sign-up and
// connecting AWS, axe (desktop and 390 px), keyboard and focus on a desktop Safari, and the
// phone layouts on an emulated iPhone (the mobile spec's contexts take its user agent, touch
// and pixel ratio). The other journeys are API-heavy set-up around ordinary forms and run on
// Chromium only, to keep the job short; the performance budget needs the Chrome DevTools
// protocol, so it is Chromium only too.
const WEBKIT_SPECS = /(onboarding|a11y|keyboard)\.spec\.ts/
const PHONE_SPECS = /mobile\.spec\.ts/

export default defineConfig({
  testDir: './tests',
  outputDir: './reports/results',
  timeout: 60_000,
  expect: { timeout: 10_000 },
  fullyParallel: true,
  forbidOnly: CI,
  retries: CI ? 1 : 0,
  // Two at a time: more parallel pages can exhaust the API's database pool (see load/README.md).
  workers: 2,
  reporter: [
    ['list'],
    ['html', { outputFolder: 'reports/html', open: 'never' }],
    ['junit', { outputFile: 'reports/junit.xml' }],
  ],
  use: {
    baseURL: WEB_URL,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
    locale: 'en-GB',
    timezoneId: 'Africa/Lagos',
  },
  projects: [
    { name: 'setup', testMatch: /global\.setup\.ts/ },
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
      dependencies: ['setup'],
    },
    {
      name: 'webkit',
      use: { ...devices['Desktop Safari'] },
      testMatch: WEBKIT_SPECS,
      dependencies: ['setup'],
    },
    {
      name: 'mobile-webkit',
      use: { ...devices['iPhone 13'] },
      testMatch: PHONE_SPECS,
      dependencies: ['setup'],
    },
  ],
  webServer: [
    {
      // Test-only API: refuses to start without CLOUDOPTICS_E2E=1 and outside the test environment.
      command: 'uv run python apps/e2e/server/serve.py',
      cwd: '../..',
      url: `${API_URL}/readyz`,
      env: {
        CLOUDOPTICS_E2E: '1',
        CLOUDOPTICS_E2E_PORT: API_PORT,
        CLOUDOPTICS_API_PUBLIC_URL: API_URL,
        CLOUDOPTICS_APP_URL: `${WEB_URL}${APP_BASE.replace(/\/$/, '')}`,
        CLOUDOPTICS_CORS_ORIGINS: JSON.stringify(WEB_ORIGINS),
      },
      reuseExistingServer: !CI,
      timeout: 180_000,
      stdout: 'pipe',
      stderr: 'pipe',
    },
    {
      command:
        process.env.E2E_SKIP_WEB_BUILD === '1'
          ? `pnpm --filter @cloudoptics/web preview --host 127.0.0.1 --port ${WEB_PORT} --strictPort`
          : `pnpm --filter @cloudoptics/web build && pnpm --filter @cloudoptics/web preview --host 127.0.0.1 --port ${WEB_PORT} --strictPort`,
      cwd: '../..',
      url: `${WEB_URL}${APP_BASE}`,
      env: { VITE_API_URL: API_URL, VITE_BASE: APP_BASE },
      reuseExistingServer: !CI,
      timeout: 240_000,
    },
  ],
})
