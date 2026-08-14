import { expect, test } from '@playwright/test';
import { openApp } from '../helpers';
import { archiveNames, keepNames, nameList, stubDashboard } from './fixtures';

/**
 * The Inbox-Zero card — ui.md screen 16, roadmap Phase 7 slice 4.
 *
 * A user who thinks "zero" means one thing and gets another has been misled, so
 * the definition is asserted verbatim and always visible. A bucket at zero must
 * render greyed, never vanish — a disappearing row reads as a bug and hides the
 * shape of the remainder. And a run that archived nothing must NEVER render in
 * the neutral state.
 */

test.use({ storageState: { cookies: [], origins: [] } });

test.describe('Phase 7 — the Inbox-Zero card', () => {
  test('states the definition and every remainder bucket, including zero-count ones', async ({
    page,
  }) => {
    await stubDashboard(page);
    await openApp(page);

    const card = page.locator('[data-testid="inbox-zero-card"]');
    await expect(card).toBeVisible();

    // The definition — always visible, not behind a tooltip or a modal.
    const definition = page.locator('[data-testid="inbox-zero-definition"]');
    await expect(definition).toBeVisible();
    await expect(definition).toContainText(
      'Inbox zero means your inbox holds only what needs a human.',
    );
    await expect(definition).toContainText('never deleted, always undoable');

    // Both category lists are INTERPOLATED from the live taxonomy (ui.md #16).
    // The expectations are derived from the fixture taxonomy, never frozen: if
    // the taxonomy changes again and the component reverts to hardcoded copy,
    // this fails. That is the entire point of these two assertions.
    await expect(page.locator('[data-testid="iz-keep-categories"]')).toContainText(
      `${nameList(keepNames())} always stay`,
    );
    await expect(page.locator('[data-testid="iz-archive-categories"]')).toHaveText(
      nameList(archiveNames()),
    );
    // Receipts is an ARCHIVE category as of Phase 7 — it must never be claimed
    // to "always stay". This guards the specific defect that shipped.
    await expect(page.locator('[data-testid="iz-keep-categories"]')).not.toContainText('Receipts');
    await expect(page.locator('[data-testid="iz-archive-categories"]')).toContainText('Receipts');

    // Headline — three numbers.
    await expect(page.locator('[data-testid="iz-applied"]')).toContainText('544');
    await expect(page.locator('[data-testid="iz-distance"]')).toContainText('0');

    // Every bucket present. `held_by_never_miss` is 0 in the fixture and must
    // still render, greyed, with its count.
    for (const bucket of [
      'needs_your_call',
      'category_keep',
      'below_threshold',
      'held_by_never_miss',
    ]) {
      const row = page.locator(`[data-testid="remainder-row-${bucket}"]`);
      await expect(row, `remainder bucket ${bucket} is not rendered`).toBeVisible();
    }
    await expect(
      page.locator('[data-testid="remainder-row-held_by_never_miss"]'),
      'a zero-count bucket was hidden instead of greyed',
    ).toHaveAttribute('data-count', '0');

    // The ledger row's names come from the live taxonomy too — every keep
    // category, and no archive category.
    const keepRow = page.locator('[data-testid="remainder-row-category_keep"]');
    for (const name of keepNames()) await expect(keepRow).toContainText(name);
    await expect(keepRow).not.toContainText('Receipts');

    // A healthy run shows no failure bar.
    await expect(page.locator('[data-testid="apply-failed-bar"]')).toHaveCount(0);
  });

  test('apply_ok === false renders the loud red bar and a working Retry — never the neutral state', async ({
    page,
  }) => {
    const applyCalls: string[] = [];
    await stubDashboard(page, {
      applyCalls,
      remainder: {
        applied: 0,
        distance_to_zero: 615,
        apply_ok: false,
        apply_failed_reason: 'RefreshError: invalid_grant while building the Gmail mutator',
      },
    });
    await openApp(page);

    const card = page.locator('[data-testid="inbox-zero-card"]');
    await expect(card).toBeVisible();
    await expect(card).toHaveAttribute('data-apply-ok', 'false');

    const bar = page.locator('[data-testid="apply-failed-bar"]');
    await expect(bar, 'a run that archived nothing rendered without the red failure bar').toBeVisible();
    await expect(bar).toContainText('could not archive');
    await expect(bar).toContainText('615');
    await expect(page.locator('[data-testid="apply-failed-reason"]')).toContainText(
      'RefreshError: invalid_grant',
    );

    // The button really calls POST /api/runs/{run_id}/apply.
    const retry = page.locator('[data-testid="retry-archiving"]');
    await expect(retry).toBeVisible();
    await retry.click();
    await expect
      .poll(() => applyCalls.length, {
        message: 'Retry archiving did not POST /api/runs/{run_id}/apply',
      })
      .toBeGreaterThan(0);
  });

  test('a dry run shows only the chip — never the red failure bar, which would be a lie', async ({
    page,
  }) => {
    await stubDashboard(page, {
      remainder: {
        applied: 0,
        distance_to_zero: 544,
        apply_ok: false,
        apply_failed_reason: null,
        dry_run: true,
      },
    });
    await openApp(page);

    await expect(page.locator('[data-testid="iz-dry-run-chip"]')).toContainText(
      'DRY RUN — nothing was archived',
    );
    // A dry run archiving nothing is the CORRECT outcome. The backend still
    // reports apply_ok=false (api.md has no dry-run exemption), so the card must
    // do the branching: chip yes, scary red "could not archive" bar no.
    await expect(
      page.locator('[data-testid="apply-failed-bar"]'),
      'a correct dry run rendered the red apply-failure bar',
    ).toHaveCount(0);
    await expect(
      page.locator('[data-testid="retry-archiving"]'),
      'Retry must be hidden in dry-run — there is nothing to retry',
    ).toHaveCount(0);
    // The ledger itself is still fully rendered.
    await expect(page.locator('[data-testid="remainder-ledger"]')).toBeVisible();
  });
});
