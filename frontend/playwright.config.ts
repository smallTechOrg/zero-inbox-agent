import { defineConfig } from '@playwright/test';

/**
 * Gate infrastructure for `pnpm --dir frontend exec playwright test tests/e2e/phase1`
 * (spec/roadmap.md). Specs live at the repo root under tests/e2e/; the path
 * argument acts as a filter against the resolved spec paths.
 *
 * Both servers are started here (reused if already running): the FastAPI
 * backend on :8001 (`uv run python -m src`) with the test-isolation guard
 * exported so a signed-in run can NEVER mutate the live mailbox, and the Next
 * dev server on :3000 (`pnpm dev`), whose rewrites proxy /api and /auth to
 * :8001. Signed-in specs additionally need ZI_SESSION (see
 * tests/e2e/helpers.ts) and otherwise skip with a loud reason.
 */
export default defineConfig({
  testDir: '../tests/e2e',
  timeout: 120_000,
  fullyParallel: false,
  workers: 1,
  reporter: [['list']],
  use: {
    baseURL: 'http://localhost:3000',
    trace: 'retain-on-failure',
  },
  webServer: [
    {
      command: 'uv run python -m src',
      cwd: '..',
      url: 'http://localhost:8001/api/health',
      reuseExistingServer: true,
      timeout: 120_000,
      stdout: 'pipe',
      stderr: 'pipe',
      env: {
        AGENT_TEST_ISOLATION: '1',
        AGENT_GMAIL_WRITE_DISABLED: '1',
      },
    },
    {
      command: 'pnpm dev',
      url: 'http://localhost:3000',
      reuseExistingServer: true,
      timeout: 120_000,
      stdout: 'pipe',
      stderr: 'pipe',
    },
  ],
});
