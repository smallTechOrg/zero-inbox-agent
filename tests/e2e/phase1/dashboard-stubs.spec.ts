import { expect, test } from '@playwright/test';
import {
  STUB_BADGE_TEXT,
  hasSession,
  openDashboard,
  section,
  stubBadges,
} from '../helpers';

/**
 * The stub convention, asserted in BOTH directions (spec/ui.md, binding):
 * every non-functional Phase-2 surface (Ledger, Costs history, Sender
 * profiles) carries the exact badge and disabled inputs — and no functional
 * panel (command strip, taxonomy, run timeline) carries it. A stub must never
 * be mistakable for a bug, and a real feature must never look like a stub.
 */

test.describe('Signed-in dashboard — Phase-2 stub badges', () => {
  test.skip(!hasSession(), 'ZI_SESSION not set — signed-in specs need a session cookie (see tests/e2e/helpers.ts). This is a loud environment gate, not a pass.');

  test.beforeEach(async ({ page }) => {
    await openDashboard(page);
  });

  test('exactly the three Phase-2 panels carry the stub badge', async ({ page }) => {
    await expect(stubBadges(page)).toHaveCount(3);
  });

  for (const [testId, heading] of [
    ['ledger-section', /ledger/i],
    ['costs-section', /costs?/i],
    ['profiles-section', /sender profiles?/i],
  ] as const) {
    test(`the ${testId} is present, badged, and disabled`, async ({ page }) => {
      const panel = section(page, testId, heading);
      await expect(panel).toBeVisible();
      await expect(panel.getByText(STUB_BADGE_TEXT, { exact: false })).toBeVisible();
      // Disabled inputs: any input/button inside a stub must not be operable.
      const inputs = panel.locator('input, button, select, textarea');
      const count = await inputs.count();
      for (let i = 0; i < count; i += 1) {
        await expect(inputs.nth(i)).toBeDisabled();
      }
    });
  }

  for (const [testId, heading] of [
    ['command-strip', /clean my inbox|resume cleaning/i],
    ['taxonomy-panel', /taxonom|categor/i],
    ['run-timeline', /runs?|timeline|history/i],
  ] as const) {
    test(`the functional ${testId} is NOT badged as a stub`, async ({ page }) => {
      const panel = section(page, testId, heading);
      await expect(panel).toBeVisible();
      await expect(panel.getByText(STUB_BADGE_TEXT, { exact: false })).toHaveCount(0);
    });
  }

  test('the taxonomy panel shows the seeded categories with Needs review pinned', async ({ page }) => {
    const panel = section(page, 'taxonomy-panel', /taxonom|categor/i);
    for (const name of ['Finance', 'Newsletters', 'Notifications', 'Personal', 'Needs review']) {
      await expect(panel.getByText(name, { exact: false }).first()).toBeVisible();
    }
  });

  test('the Clean my inbox button is real and enabled (or explains why not)', async ({ page }) => {
    const button = page.getByRole('button', { name: /clean my inbox|resume cleaning/i }).first();
    await expect(button).toBeVisible();
    if (await button.isDisabled()) {
      // Disabled is only allowed WITH a visible reason (a run is active).
      await expect(page.locator('body')).toContainText(/running|active|in progress/i);
    }
  });
});
