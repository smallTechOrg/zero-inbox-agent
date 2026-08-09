import { defineConfig, devices } from '@playwright/test';

/**
 * Zero Inbox Agent E2E config.
 *
 * The app is a Next.js static export served by FastAPI at http://localhost:8001/app/
 * (note the /app/ and the trailing slash). Start it with:
 *
 *   uv run alembic upgrade head
 *   cd frontend && pnpm build && cd ..
 *   uv run python -m src
 *
 * If a server is already listening on 8001 it is reused; otherwise Playwright starts one.
 */
export default defineConfig({
  testDir: './tests/e2e',
  timeout: 60_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: 'line',
  use: {
    baseURL: 'http://localhost:8001',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: {
    command: 'uv run python -m src',
    url: 'http://localhost:8001/health',
    reuseExistingServer: true,
    timeout: 120_000,
    stdout: 'pipe',
    stderr: 'pipe',
  },
});
