import { expect, test } from '@playwright/test';
import { APP_URL } from '../helpers';
import { stubApp } from './fixtures';

/**
 * Screens 20–21 — sign-in lands in a three-step first run, and the dry-run
 * copy tells the truth about WHY nothing was archived.
 *
 * Every API call is intercepted (see fixtures.ts): no run is started, no
 * setting on the real server is changed, no mailbox is touched.
 */

test.describe('Phase 8 — first run (onboarding)', () => {
  test('a signed-in user with no mailbox lands on step 1, never the console', async ({ page }) => {
    await stubApp(page, { connected: false, run: null });
    await page.goto(APP_URL, { waitUntil: 'domcontentloaded' });

    await expect(page.getByTestId('onboarding')).toBeVisible();
    await expect(page.getByTestId('onboarding-steps')).toBeVisible();
    // The step indicator names the step in words, never a bare dot row.
    await expect(page.getByTestId('onboarding-steps')).toContainText('Connect your mailbox');
    await expect(page.getByTestId('onboarding-steps')).toContainText('you are here');
    await expect(page.getByTestId('connect-gmail')).toBeVisible();

    // The false Phase-1 promise is gone from the connect step.
    await expect(page.locator('body')).not.toContainText('Nothing is changed until you say so');
    await expect(page.locator('body')).not.toContainText('never in Phase 1');

    // No console surface behind it.
    await expect(page.getByTestId('cluster-row')).toHaveCount(0);
    await expect(page.getByTestId('inbox-zero-card')).toHaveCount(0);
  });

  test('dry run is on by default, and every statement about it is derived from the real setting', async ({
    page,
  }) => {
    await stubApp(page, { run: null, dryRun: true });
    await page.goto(APP_URL, { waitUntil: 'domcontentloaded' });

    // The banner states the real mechanism — suppression — not a vague reassurance.
    const banner = page.getByTestId('dry-run-banner');
    await expect(banner).toBeVisible();
    await expect(banner).toContainText('Dry run — Gmail mutations are suppressed');
    await expect(banner).toContainText('No message is archived, labelled, deleted or moved.');

    // Step 2 says what will happen before it happens.
    await expect(page.getByTestId('what-happens-next')).toContainText(
      'We archive only what we’re confident about.',
    );
    await expect(page.getByTestId('step2-dry-run-state')).toContainText(
      'Dry run is on — this pass will classify everything and archive nothing.',
    );

    // Step 3: the PINNED LINE must blame dry run, not the reviewer.
    await page.getByTestId('onboarding-watch').click();
    const pinned = page.getByTestId('dry-run-pinned-line');
    await expect(pinned).toBeVisible();
    await expect(pinned).toContainText('Dry run is on — nothing will be archived this pass');
    await expect(pinned).toContainText('Every decision is recorded');

    // The reviewer explanation would be a plausible-sounding lie in this state.
    await expect(page.getByTestId('nothing-archived-yet')).toHaveCount(0);
    await expect(page.locator('body')).not.toContainText(
      'the reviewer checks every decision first',
    );

    // The moment-of-trust callout is real or absent — never fabricated.
    await expect(page.getByTestId('never-miss-callout')).toHaveCount(0);
  });

  test('the dry-run control is a LIVE two-way control, not a permanently disabled button', async ({
    page,
  }) => {
    const log = await stubApp(page, { run: null, dryRun: true });
    await page.goto(APP_URL, { waitUntil: 'domcontentloaded' });

    const control = page.getByTestId('start-in-dry-run');
    await expect(control).toBeVisible();
    await expect(control).toBeEnabled();
    // It offers the direction the user is NOT in — dry run is already on.
    await expect(control).toHaveText('Dry run is on — run for real instead');

    // The dangerous direction arms a confirm step first, and can be backed out of.
    await control.click();
    await expect(control).toHaveText('Confirm — turn dry run off and archive for real');
    await expect(page.getByTestId('cancel-turn-dry-run-off')).toBeVisible();
    expect(log.settingsPatches, 'arming the confirm must not write the setting').toEqual([]);

    await page.getByTestId('cancel-turn-dry-run-off').click();
    await expect(control).toHaveText('Dry run is on — run for real instead');
    expect(log.settingsPatches).toEqual([]);
  });

  test('with dry run off the control offers dry run, and really writes the setting', async ({
    page,
  }) => {
    const log = await stubApp(page, { run: null, dryRun: false });
    await page.goto(APP_URL, { waitUntil: 'domcontentloaded' });

    // Dry run off ⇒ the banner says so in words, and step 2 states the consequence.
    await expect(page.getByTestId('dry-run-banner')).toContainText(
      'Live — actions will modify your Gmail',
    );
    await expect(page.getByTestId('step2-dry-run-state')).toContainText(
      'Dry run is off — this pass will really archive what it is confident about',
    );

    const control = page.getByTestId('start-in-dry-run');
    await expect(control).toHaveText('Start in dry-run instead');
    await expect(control).toBeEnabled();

    // One click in the SAFE direction — it is wired to PATCH /api/settings for real.
    await control.click();
    await expect
      .poll(() => log.settingsPatches, { message: 'the control never called PATCH /api/settings' })
      .toEqual([{ dry_run: true }]);
    await expect(page.getByTestId('step2-dry-run-state')).toContainText('Dry run is on');
  });

  test('step 3 shows the live feed and a run that has not archived anything says so honestly', async ({
    page,
  }) => {
    await stubApp(page, { run: null, dryRun: false });
    await page.goto(APP_URL, { waitUntil: 'domcontentloaded' });
    await page.getByTestId('onboarding-watch').click();

    await expect(page.getByTestId('live-run-feed')).toBeVisible();
    // Dry run off and nothing archived yet: the reviewer line is the TRUE reason here.
    await expect(page.getByTestId('nothing-archived-yet')).toContainText(
      'Nothing has been archived yet — the reviewer checks every decision first.',
    );
    await expect(page.getByTestId('dry-run-pinned-line')).toHaveCount(0);
    // The undo affordance belongs to the archiving line, which is not showing yet.
    await expect(page.getByTestId('undo-this-run')).toHaveCount(0);
  });
});
