import { expect, test } from '@playwright/test';
import { APP_URL, assertPageIsStyled } from '../helpers';

/**
 * The signed-out front door (spec/ui.md § Signed-out state): hero, the
 * "Sign in with Google" button, and the honesty band — what the agent reads
 * (headers + snippet, never bodies), that everything is undoable, INBOX-only.
 * Runs anonymously; no session needed.
 */

test.describe('Signed-out front door', () => {
  test('the dashboard is served and genuinely styled', async ({ page }) => {
    await page.goto(APP_URL, { waitUntil: 'domcontentloaded' });
    await assertPageIsStyled(page);
    await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
  });

  test('the Sign in with Google button is present', async ({ page }) => {
    await page.goto(APP_URL, { waitUntil: 'domcontentloaded' });
    const signIn = page
      .getByRole('link', { name: /sign in with google/i })
      .or(page.getByRole('button', { name: /sign in with google/i }));
    await expect(signIn.first()).toBeVisible();
  });

  test('the honesty band states the privacy and undo guarantees', async ({ page }) => {
    await page.goto(APP_URL, { waitUntil: 'domcontentloaded' });
    const body = page.locator('body');
    // never bodies…
    await expect(body).toContainText(/never|no email bodies|bodies/i);
    // …everything undoable…
    await expect(body).toContainText(/undo/i);
    // …INBOX-only.
    await expect(body).toContainText(/inbox/i);
  });

  test('an anonymous visitor never sees a raw error or blank screen', async ({ page }) => {
    await page.goto(APP_URL, { waitUntil: 'domcontentloaded' });
    await expect(page.locator('body')).not.toBeEmpty();
    await expect(page.locator('body')).not.toContainText(/signed_out|"ok":\s*false/);
  });
});
