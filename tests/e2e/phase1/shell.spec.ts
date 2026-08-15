import { expect, test } from '@playwright/test';
import { assertPageIsStyled, APP_URL } from '../helpers';

/**
 * The Phase-1 smoke test, rewritten for the app as it is in Phase 9.
 *
 * The original `tests/e2e/smoke.spec.ts` asserted the Phase-1 *console* for an
 * anonymous visitor — the DRY RUN banner, the COMING SOON stub chips and the
 * left-rail stub buttons. Phase 8 deliberately replaced that: an anonymous
 * visitor lands on the homepage, and Phase 9 has **no labelled stubs left at
 * all**. Those assertions were asserting a product that no longer exists, so
 * Phase 9 takes them on rather than leaving them red (spec/roadmap.md § Phase 9,
 * slice 7).
 *
 * What survives is what was never phase-specific and is still load-bearing: the
 * app is served, it is genuinely styled (a built bundle, not a DOM dump), and
 * the API answers in the standard envelope — including on its error path. This
 * runs live and read-only against the supervised server on :8001.
 */

test.describe('The served app shell (rewritten Phase-1 smoke)', () => {
  test('the app is served at /app/ and is genuinely styled', async ({ page }) => {
    await page.goto(APP_URL, { waitUntil: 'domcontentloaded' });
    await expect(page).toHaveURL(/\/app\/?$/);
    await expect(page.locator('body')).not.toBeEmpty();
    await assertPageIsStyled(page);
  });

  test('an anonymous visitor gets a page, never a blank screen or a raw error', async ({ page }) => {
    await page.goto(APP_URL, { waitUntil: 'domcontentloaded' });
    // A real landmark, not a character count: the shell must resolve to
    // something a person can read and act on.
    await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
    await expect(page.getByRole('banner')).toBeVisible();
    await expect(page.locator('body')).not.toContainText('Application error');
    await expect(page.locator('body')).not.toContainText('Internal Server Error');
  });

  test('the health endpoint is live and /api/me answers in the standard envelope', async ({
    page,
  }) => {
    const health = await page.request.get('http://localhost:8001/health');
    expect(health.ok()).toBe(true);

    const me = await page.request.get('http://localhost:8001/api/me');
    const body = await me.json();
    expect(body).toHaveProperty('data');
    expect(body).toHaveProperty('error');
    if (!me.ok()) {
      // The error path is an envelope too — never an HTML 500 page.
      expect(me.status()).toBe(401);
      expect(body.error.code).toBe('unauthenticated');
    }
  });
});
