import { test, expect } from '@playwright/test';
import { openApp, isMailboxConnected } from '../helpers';

/**
 * Phase 2 — confirms every capability still deferred to Phase 3 remains a visibly
 * labelled, non-functional, non-erroring stub: Rules, Chat, Digest, Backlog, Cost
 * panel, Model dropdown, "Create Gmail filter", "Draft reply", Unsubscribe
 * suggestions, Stale threads, and the new Phase-2-introduced "Taxonomy editor" stub
 * in Settings. This is not merely a re-run of Phase 1's smoke test: it asserts these
 * stay stubbed even though Phase 2 has landed real actions elsewhere on the page, so
 * a user can never mistake a genuinely unbuilt feature for a regression.
 */
test.describe('Phase 2 — remaining stubs stay labelled and inert', () => {
  test.beforeEach(async ({ page }) => {
    await openApp(page);
  });

  test('left-rail stubs (Rules, Chat, Digest, Backlog, Cost) are disabled and labelled COMING SOON', async ({
    page,
  }) => {
    // Stub nav buttons carry an aria-label of "<Label> (coming soon, Phase N)" —
    // match unanchored so this doesn't couple to that exact phrasing.
    for (const name of [/Rules/i, /Chat/i, /Digest/i, /Backlog/i, /Cost/i]) {
      const button = page.getByRole('button', { name });
      await expect(button).toBeVisible();
      await expect(button).toBeDisabled();
      await expect(button).toHaveAttribute('aria-disabled', 'true');

      // Clicking a disabled button is a no-op in the DOM, but assert clicking never
      // navigates away or throws — it stays on the same dashboard shell.
      await button.click({ force: true }).catch(() => undefined);
      await expect(page).toHaveURL(/\/app\/?$/);
    }

    const chips = page.getByText(/coming soon/i);
    expect(await chips.count()).toBeGreaterThan(0);
  });

  test('the right rail "not built yet" panels are present and labelled for their real Phase', async ({
    page,
  }) => {
    const aside = page.getByRole('complementary', { name: /coming soon/i });
    await expect(aside).toBeVisible();

    for (const title of [
      'Cost panel',
      'Model dropdown',
      'Backlog cleanup',
      'Daily digest',
      'Unsubscribe suggestions',
      'Stale threads',
    ]) {
      await expect(aside.getByText(title, { exact: false })).toBeVisible();
    }

    // VIP list and priorities profile graduated out of the stub panel in Phase 2 —
    // confirm the aside says so rather than still stubbing them.
    await expect(
      aside.getByText(/VIP list and priorities profile are real now/i),
    ).toBeVisible();

    // No interactive control inside a stub panel is enabled.
    const stubControls = aside.locator('button, input, select');
    const count = await stubControls.count();
    for (let i = 0; i < count; i += 1) {
      await expect(stubControls.nth(i)).toBeDisabled();
    }
  });

  test('the header "Model: auto" control is a disabled stub, not a working dropdown', async ({
    page,
  }) => {
    const modelStub = page.getByRole('button', { name: /Model: auto/i });
    await expect(modelStub).toBeVisible();
    await expect(modelStub).toBeDisabled();
  });

  test('Settings shows the Taxonomy editor as a Phase 3 stub', async ({ page }) => {
    await page.getByRole('button', { name: /^Settings$/i }).click();
    await expect(page.locator('#settings-panel')).toBeVisible();

    const taxonomyStub = page.getByText(/Taxonomy editor/i);
    await expect(taxonomyStub).toBeVisible();
    await expect(page.getByText(/coming soon/i).first()).toBeVisible();
  });

  test('"Create Gmail filter" and "Draft reply" inside a thread detail are disabled stubs', async ({
    page,
  }) => {
    const connected = await isMailboxConnected(page);
    test.skip(
      !connected,
      'NO MAILBOX CONNECTED — export ZI_SESSION=<zi_session cookie value> to seed the session ' +
        'headlessly (see tests/e2e/helpers.ts). This journey is unverified, not passing.',
    );

    const clusters = page
      .locator('[data-testid="cluster-row"]')
      .or(page.getByRole('listitem').filter({ hasText: /\d+\s+threads?/i }));
    await expect(clusters.first()).toBeVisible({ timeout: 180_000 });
    await clusters.first().click();

    const threads = page.locator('[data-testid="thread-row"]');
    await expect(threads.first()).toBeVisible({ timeout: 30_000 });
    await threads.first().click();

    for (const label of [/Create Gmail filter/i, /Draft reply/i]) {
      const control = page.getByRole('button', { name: label });
      await expect(control.first()).toBeVisible();
      await expect(control.first()).toBeDisabled();
    }
  });
});
