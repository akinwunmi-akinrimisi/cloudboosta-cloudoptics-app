import { defineConfig, devices } from '@playwright/test'

// The suite runs the real web app (production build, `vite preview`) against the real API,
// PostgreSQL and inline jobs. Only AWS is replaced, by apps/e2e/server/fake_aws.py; see
// apps/e2e/README.md. Both servers start here unless already running (locally).

const API_URL = process.env.E2E_API_URL ?? 'http://127.0.0.1:8000'
const WEB_URL = process.env.E2E_WEB_URL ?? 'http://127.0.0.1:4173'
const CI = Boolean(process.env.CI)

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
  ],
  webServer: [
    {
      // Test-only API: refuses to start without CLOUDOPTICS_E2E=1 and outside the test environment.
      command: 'uv run python apps/e2e/server/serve.py',
      cwd: '../..',
      url: `${API_URL}/readyz`,
      env: { CLOUDOPTICS_E2E: '1' },
      reuseExistingServer: !CI,
      timeout: 180_000,
      stdout: 'pipe',
      stderr: 'pipe',
    },
    {
      command:
        process.env.E2E_SKIP_WEB_BUILD === '1'
          ? 'pnpm --filter @cloudoptics/web preview --host 127.0.0.1 --port 4173 --strictPort'
          : 'pnpm --filter @cloudoptics/web build && pnpm --filter @cloudoptics/web preview --host 127.0.0.1 --port 4173 --strictPort',
      cwd: '../..',
      url: WEB_URL,
      env: { VITE_API_URL: API_URL },
      reuseExistingServer: !CI,
      timeout: 240_000,
    },
  ],
})
