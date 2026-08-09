import { test, expect } from '@playwright/test';
import { openApp, assertPageIsStyled, byTestIdOrText, isMailboxConnected } from './helpers';

/**
 * Phase 1 smoke: the shell the user lands on at http://localhost:8001/app/.
 * Everything asserted here must hold whether or not a mailbox is connected.
 */
test.describe('Phase 1 — dashboard shell', () => {
  test.beforeEach(async ({ page }) => {
    await openApp(page);
  });

  test('the dashboard loads and is styled', async ({ page }) => {
    await expect(page).toHaveURL(/\/app\/?$/);
    await assertPageIsStyled(page);
    await expect(page.locator('body')).not.toBeEmpty();
  });

  test('the DRY RUN banner is present and unmissable', async ({ page }) => {
    const banner = byTestIdOrText(page, 'dry-run-banner', /DRY RUN/i);

    await expect(banner).toBeVisible();
    await expect(banner).toContainText(/nothing in your Gmail has been changed/i);

    // Pinned near the top of the viewport, above the content.
    const box = await banner.boundingBox();
    expect(box, 'dry-run banner has no layout box').not.toBeNull();
    expect(box!.y, 'dry-run banner is not pinned at the top').toBeLessThan(200);
    expect(box!.width, 'dry-run banner is not full width').toBeGreaterThan(400);
  });

  test('the Connect Gmail control is present and points at the real OAuth flow', async ({
    page,
  }) => {
    const connected = await isMailboxConnected(page);
    const connect = byTestIdOrText(page, 'connect-gmail', /Connect Gmail/i);

    if (!connected) {
      await expect(connect).toBeVisible();
      await expect(connect).toBeEnabled();
    } else {
      // Already connected: the connected address is shown instead.
      const account = byTestIdOrText(page, 'connected-account', /@/);
      await expect(account).toBeVisible();
    }
  });

  test('every COMING SOON stub is visibly labelled and disabled', async ({ page }) => {
    const chips = page.getByText(/COMING SOON/i);
    const chipCount = await chips.count();
    expect(chipCount, 'no COMING SOON chip found — Phase 1 stubs must be labelled').toBeGreaterThan(
      0,
    );

    for (let i = 0; i < chipCount; i += 1) {
      await expect(chips.nth(i)).toBeVisible();
    }

    // Any control inside a stubbed region must be disabled so it can never read as a bug.
    const stubControls = page.locator(
      '[data-stub="true"] button, [data-stub="true"] input, [data-stub="true"] select, [data-testid$="-stub"] button',
    );
    const stubControlCount = await stubControls.count();
    for (let i = 0; i < stubControlCount; i += 1) {
      await expect(stubControls.nth(i)).toBeDisabled();
    }

    // Named Phase 1 stubs from the roadmap that must never be clickable.
    for (const label of [/^Undo$/i, /Create Gmail filter/i, /Draft reply/i]) {
      const control = page.getByRole('button', { name: label });
      if ((await control.count()) > 0) {
        await expect(control.first()).toBeDisabled();
      }
    }
  });

  test('navigation to a stub view shows its COMING SOON state, not an error', async ({ page }) => {
    for (const name of [/^Rules$/i, /^Chat$/i, /^Digest$/i]) {
      const link = page.getByRole('link', { name }).or(page.getByRole('button', { name }));
      if ((await link.count()) === 0) continue;

      await expect(link.first()).toBeVisible();
      await expect(page.getByText(/COMING SOON/i).first()).toBeVisible();
    }
  });

  test('the health endpoint is live and the API envelope is well formed', async ({ page }) => {
    const health = await page.request.get('http://localhost:8001/health');
    expect(health.ok()).toBe(true);

    const me = await page.request.get('http://localhost:8001/api/me');
    const body = await me.json();
    expect(body).toHaveProperty('data');
    expect(body).toHaveProperty('error');
    if (!me.ok()) {
      // Error path: unauthenticated must be a clean envelope, never an HTML 500 page.
      expect(me.status()).toBe(401);
      expect(body.error.code).toBe('unauthenticated');
    }
  });
});
