import { expect, test } from '@playwright/test';
import { openApp, openSettings, stubApp } from './fixtures';

/**
 * Phase 9, user-test step 7 (spec/roadmap.md § How the user tests it), screen 29
 * (spec/ui.md).
 *
 * Setting **People** / **Urgent** / **Legal** / **Important** to
 * archive-by-default is still REFUSED, and the disabled control now carries the
 * explanation rather than a bare grey box:
 *
 *   "kept for you — archived under its own label, never swept as a category."
 *
 * This is the distinction the whole phase rests on: a never-miss thread leaves
 * the inbox INTO its own label; the never-miss *category* is never swept.
 *
 * Every route is intercepted, and the stub answers any non-GET on
 * `/api/categories*` with 403 — a spec in this directory can never mutate a
 * category, stubbed or real.
 */

const NOTE = 'kept for you — archived under its own label, never swept as a category.';

test.describe('Phase 9 — the never-archive guard is kept and explained', () => {
  test('every never-miss category refuses archive-by-default and says why', async ({ page }) => {
    const log = await stubApp(page);
    await openApp(page);
    await openSettings(page);

    const rows = page.getByTestId('category-row');
    await expect(rows.first()).toBeVisible();

    for (const name of ['People', 'Urgent', 'Legal', 'Important']) {
      const row = rows.filter({ hasText: name }).first();
      const select = row.getByTestId('category-action-select');
      await expect(select, `${name} has no action control`).toBeVisible();
      await expect(select, `${name} is not marked never-archive`).toHaveAttribute(
        'data-never-archive',
        'true',
      );
      await expect(
        select.locator('option[value="archive"]'),
        `${name} can still be set to archive`,
      ).toBeDisabled();
      await expect(row.getByTestId('never-archive-note'), `${name} gives no reason`).toHaveText(NOTE);
    }

    // A category that is genuinely sweepable is NOT blocked — the guard is
    // narrow, not a blanket that would make the editor useless.
    const notifications = rows.filter({ hasText: 'Notifications' }).first();
    await expect(notifications.getByTestId('category-action-select')).toHaveAttribute(
      'data-never-archive',
      'false',
    );
    await expect(notifications.getByTestId('never-archive-note')).toHaveCount(0);

    // Nothing on this screen wrote anything.
    expect(
      log.requests.filter(r => r.startsWith('PATCH /api/categories') || r.startsWith('POST /api/categories')),
      'the taxonomy editor mutated a category just by being opened',
    ).toHaveLength(0);
  });

  test('trying to select archive on People changes nothing and sends no request', async ({
    page,
  }) => {
    const log = await stubApp(page);
    await openApp(page);
    await openSettings(page);

    await expect(page.getByTestId('category-row').first()).toBeVisible();
    const row = page.getByTestId('category-row').filter({ hasText: 'People' }).first();
    const select = row.getByTestId('category-action-select');
    await expect(select).toHaveValue('keep');

    // The option is disabled, so this cannot succeed — Playwright waits for an
    // actionable option and never gets one, which IS the guarantee. The short
    // timeout keeps that a fast assertion rather than a hang.
    await select.selectOption('archive', { timeout: 3_000 }).then(
      () => {
        throw new Error('archive was selectable on a never-miss category');
      },
      () => undefined,
    );
    await expect(select).toHaveValue('keep');
    expect(
      log.requests.filter(r => r.includes('PATCH /api/categories')),
      'a refused archive still reached the server',
    ).toHaveLength(0);
  });
});
