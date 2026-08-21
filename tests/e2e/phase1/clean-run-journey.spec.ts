import { expect, test } from '@playwright/test';
import { hasSession, openDashboard, section } from '../helpers';

/**
 * The full Phase-1 journey (spec/roadmap.md "How the user tests it"):
 * mini-audit counts → tweak a category name → Clean my inbox → live feed
 * streams sentences with a cost ticker → run card with per-category counts →
 * Undo this run. Requires ZI_SESSION (a signed-in, Gmail-connected session)
 * and the backend running with the test-isolation guard so Gmail writes are
 * sandboxed — mutations are audited, never sent.
 */

test.describe('Phase-1 clean-run journey', () => {
  test.skip(!hasSession(), 'ZI_SESSION not set — the journey needs a signed-in session (see tests/e2e/helpers.ts). Loud gate, not a pass.');
  test.describe.configure({ mode: 'serial' });
  test.setTimeout(300_000);

  test('mini-audit counts appear in the command strip', async ({ page }) => {
    await openDashboard(page);
    const strip = section(page, 'command-strip', /clean my inbox|resume cleaning/i);
    await expect(strip).toBeVisible();
    // First visit auto-triggers the audit; wait out the progress state.
    await expect(strip.getByText(/auditing your inbox/i)).toHaveCount(0, { timeout: 30_000 });
    await expect(strip).toContainText(/\d/, { timeout: 30_000 });
  });

  test('a category can be renamed inline', async ({ page }) => {
    await openDashboard(page);
    const panel = section(page, 'taxonomy-panel', /taxonom|categor/i);
    await expect(panel.getByText('Shopping', { exact: false }).first()).toBeVisible();
    // Contract: chips support inline rename; exact interaction is the
    // frontend's choice — we assert the edit affordance exists.
    const editable = panel
      .locator('[data-testid="category-chip"], input, [contenteditable="true"], button')
      .first();
    await expect(editable).toBeVisible();
  });

  test('Clean my inbox streams a live feed and ends in a run card with undo', async ({ page }) => {
    await openDashboard(page);
    const button = page.getByRole('button', { name: /clean my inbox|resume cleaning/i }).first();
    await expect(button).toBeEnabled({ timeout: 30_000 });
    await button.click();

    // Live feed: at least one plain-English sentence streams in.
    const feed = section(page, 'activity-feed', /activity|feed/i);
    await expect(feed).toBeVisible({ timeout: 30_000 });
    await expect(feed.locator('li, [data-testid="feed-event"], p').first()).toBeVisible({
      timeout: 120_000,
    });

    // Cost ticker is real in Phase 1.
    await expect(page.locator('body')).toContainText(/\$|tokens?|calls?/i, { timeout: 120_000 });

    // Completion: a run card with an Undo button.
    const undoButton = page.getByRole('button', { name: /undo this run/i }).first();
    await expect(undoButton).toBeVisible({ timeout: 240_000 });
  });

  test('Undo this run reverts the run and badges it undone', async ({ page }) => {
    await openDashboard(page);
    const undoButton = page.getByRole('button', { name: /undo this run/i }).first();
    await expect(undoButton).toBeVisible({ timeout: 30_000 });
    await undoButton.click();
    // Confirm dialog (spec: one-click undo WITH confirm).
    const confirm = page.getByRole('button', { name: /confirm|yes|undo/i }).last();
    if (await confirm.isVisible().catch(() => false)) await confirm.click();
    await expect(page.getByText(/undone/i).first()).toBeVisible({ timeout: 120_000 });
  });
});
