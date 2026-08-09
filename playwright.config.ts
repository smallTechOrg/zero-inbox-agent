import { defineConfig, devices } from '@playwright/test';
import { sessionStorageState } from './tests/e2e/helpers';

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
 *
 * To run the connected-mailbox triage journey headlessly, export ZI_SESSION with a
 * valid `zi_session` cookie value (see tests/e2e/helpers.ts#sessionStorageState).
 * Without it those tests skip gracefully with an explicit reason.
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
    // Seeds the zi_session cookie from $ZI_SESSION (undefined = anonymous).
    storageState: sessionStorageState(),
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
