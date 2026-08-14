import { expect, test } from '@playwright/test';
import { openApp } from '../helpers';
import { stubDashboard } from './fixtures';

/**
 * Honest autonomy controls — ui.md screen 17.
 *
 * The auto-act slider was dead config until Phase 7: persisted, defaulted,
 * sliderised and read by no code path, with a `0.95` default sitting above the
 * model's entire measured range (0 of 615 archive proposals scored >= 0.95).
 * It is now real, so it must state its consequence — above 0.90 the agent
 * archives almost nothing and the inbox never reaches zero.
 */

test.use({ storageState: { cookies: [], origins: [] } });

async function openSettings(page: import('@playwright/test').Page) {
  await openApp(page);
  await page.getByRole('button', { name: 'Settings' }).first().click();
  await expect(page.locator('[data-testid="slider-auto-act-threshold"]')).toBeVisible();
}

test.describe('Phase 7 — autonomy controls', () => {
  test('the slider is relabelled and always states its calibration', async ({ page }) => {
    await stubDashboard(page, { autoActThreshold: 0.8 });
    await openSettings(page);

    await expect(page.getByText('Act on its own above this confidence')).toBeVisible();

    // The floor is named as the hard lower bound the slider can only raise.
    const sub = page.locator('[data-testid="autonomy-sub-label"]');
    await expect(sub).toContainText('never-miss floor');
    await expect(sub).toContainText('0.75');
    await expect(sub).toContainText('can only raise the bar, never lower it');

    // The calibration note is always visible, not conditional.
    await expect(page.locator('[data-testid="autonomy-calibration-note"]')).toContainText('0.94');

    // The slider's minimum really is the confidence floor.
    await expect(page.locator('[data-testid="slider-auto-act-threshold"]')).toHaveAttribute(
      'min',
      '0.75',
    );

    // At the sane default there is no warning.
    await expect(page.locator('[data-testid="autonomy-ceiling-warning"]')).toHaveCount(0);
  });

  test('above 0.90 a red warning names the consequence', async ({ page }) => {
    await stubDashboard(page, { autoActThreshold: 0.95 });
    await openSettings(page);

    const warning = page.locator('[data-testid="autonomy-ceiling-warning"]');
    await expect(
      warning,
      'a slider above the model ceiling rendered no warning — the user would set a value that archives nothing',
    ).toBeVisible();
    await expect(warning).toContainText(
      'At this setting the agent will archive almost nothing — your inbox will not reach zero.',
    );
  });

  test('per-category bars are editable, and inert on keep categories with the reason shown', async ({
    page,
  }) => {
    await stubDashboard(page);
    await openSettings(page);

    const rows = page.locator('[data-testid="category-row"]');
    await expect(rows.first()).toBeVisible();

    // People is `keep` — its bar is disabled and says why, rather than looking broken.
    const peopleRow = rows.filter({ hasText: 'People' }).first();
    const peopleInput = peopleRow.locator('[data-testid="category-threshold-input"]');
    await expect(peopleInput).toBeDisabled();
    await expect(
      peopleRow.locator('[data-testid="category-threshold-inert-note"]'),
    ).toContainText('never archived automatically');

    // Outreach is `archive` — its bar is real and carries the seeded 0.85.
    const outreachRow = rows.filter({ hasText: 'Outreach' }).first();
    const outreachInput = outreachRow.locator('[data-testid="category-threshold-input"]');
    await expect(outreachInput).toBeEnabled();
    await expect(outreachInput).toHaveValue('0.85');
  });
});
