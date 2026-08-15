import { expect, test } from '@playwright/test';
import { openApp, openSettings, PROPOSAL, stubApp } from './fixtures';

/**
 * Phase 9, user-test steps 2 and 3 (spec/roadmap.md § How the user tests it),
 * screen 26 (spec/ui.md).
 *
 * Settings → Taxonomy → "Rebuild my categories from my mail" → a proposal built
 * from the user's OWN senders, by name, with thread counts and evidence →
 * editable → Approve → offered "Re-organise everything".
 *
 * Every route is intercepted (see fixtures.ts): no category is created, no
 * proposal is applied and no re-organisation is started on the live account.
 */

test.describe('Phase 9 — taxonomy discovery', () => {
  test('the proposal names the user\'s own senders with thread counts and evidence', async ({
    page,
  }) => {
    const log = await stubApp(page);
    await openApp(page);
    await openSettings(page);

    const discovery = page.getByTestId('taxonomy-discovery');
    await expect(discovery).toBeVisible();

    // Before the click, nothing has been asked of the server and nothing shown.
    expect(log.requests.filter(r => r.includes('/api/taxonomy/discover'))).toHaveLength(0);
    await expect(page.getByTestId('proposal-list')).toHaveCount(0);

    await discovery.getByTestId('rebuild-taxonomy-btn').click();

    const rows = page.getByTestId('proposal-row');
    await expect(rows).toHaveCount(PROPOSAL.length);

    // Discovery mutates NOTHING — the proposal is a read.
    expect(log.taxonomyApplies, 'discover must not apply anything').toHaveLength(0);

    // The user's real senders, by name, with their thread counts. This is the
    // whole point of step 2: a generic list is a failure, not a pass.
    const evidence = page.getByTestId('proposal-evidence-sender');
    for (const address of [
      'notification@facebookmail.com',
      'no-reply@entertainment.bookmyshow.com',
      'contact@jagrititheatre.com',
      'no_reply@email.apple.com',
      'service@paypal.com',
    ]) {
      await expect(
        evidence.filter({ hasText: address }),
        `evidence sender ${address} is missing from the proposal`,
      ).toHaveCount(1);
    }
    await expect(evidence.filter({ hasText: 'notification@facebookmail.com' })).toContainText('503');
    await expect(evidence.filter({ hasText: 'contact@jagrititheatre.com' })).toContainText('334');

    // Each row is a diff with its own absorbed-thread count.
    const facebook = page.locator('[data-testid="proposal-row"][data-key="social-facebook"]');
    await expect(facebook).toHaveCount(1);
    await expect(facebook.getByTestId('proposal-diff-badge')).toHaveText(/KEEP|RENAME|MERGE|ADD|RETIRE/);
    await expect(facebook.getByTestId('proposal-covered')).toContainText('1,586');

    // Coverage, and the 16 no-fit / 46 low-confidence threads it resolves.
    const coverage = page.getByTestId('discovery-coverage');
    await expect(coverage).toContainText('9,812');
    await expect(coverage).toContainText('16 of 16');
    await expect(coverage).toContainText('46 of 46');

    // What is NOT covered is named, never rounded away.
    await expect(page.getByTestId('discovery-remainder')).toContainText('524');
  });

  test('the proposal is editable and Approve sends the edited version — and only then writes', async ({
    page,
  }) => {
    const log = await stubApp(page);
    await openApp(page);
    await openSettings(page);
    await page.getByTestId('rebuild-taxonomy-btn').click();
    await expect(page.getByTestId('proposal-row').first()).toBeVisible();

    // "Nothing is mutated until Approve" is stated on screen, not just true.
    await expect(page.getByTestId('approve-nothing-changes-note')).toContainText(
      /nothing has changed yet/i,
    );

    const firstName = page.getByTestId('proposal-name-input').first();
    await firstName.fill('Facebook noise');

    // Exclude a row: the user is in charge of the taxonomy, not the model.
    const rows = page.getByTestId('proposal-row');
    await rows.nth(2).getByTestId('proposal-include').uncheck();

    expect(log.taxonomyApplies, 'editing must not write anything').toHaveLength(0);

    await page.getByTestId('approve-taxonomy-btn').click();
    await expect(page.getByTestId('reorg-offer')).toBeVisible();

    expect(log.taxonomyApplies).toHaveLength(1);
    const sent = log.taxonomyApplies[0].proposal as { key: string; name: string }[];
    expect(sent, 'the excluded row was still sent').toHaveLength(PROPOSAL.length - 1);
    expect(sent.map(c => c.name), 'the user\'s edit was not sent').toContain('Facebook noise');
    expect(sent.map(c => c.key)).not.toContain(PROPOSAL[2].key);
  });

  test('after Approve, "Re-organise everything" is offered with its real scope and its undo promise', async ({
    page,
  }) => {
    const log = await stubApp(page);
    await openApp(page);
    await openSettings(page);
    await page.getByTestId('rebuild-taxonomy-btn').click();
    await expect(page.getByTestId('proposal-row').first()).toBeVisible();
    await page.getByTestId('approve-taxonomy-btn').click();

    const offer = page.getByTestId('reorg-offer');
    await expect(offer).toBeVisible();

    const scope = page.getByTestId('reorg-offer-scope');
    // Every past decision, including mail already archived — not the inbox, not a sample.
    await expect(scope).toContainText('10,336');
    await expect(scope).toContainText(/already archived/i);
    await expect(scope).toContainText(/never pulled back into your inbox/i);
    // Trust invariant: never presented as deletion, always undoable.
    await expect(scope).toContainText(/nothing is deleted/i);
    await expect(scope).toContainText(/undone in one click/i);

    expect(log.reorgStarts, 'the offer must not start the job by itself').toHaveLength(0);
    await expect(page.getByTestId('reorg-everything-btn')).toBeEnabled();
  });

  test('a partial proposal renders amber and says so — it is never passed off as derived', async ({
    page,
  }) => {
    await stubApp(page, {
      partial: true,
      partialReason: 'The model was unavailable, so only deterministic sender signals were used.',
    });
    await openApp(page);
    await openSettings(page);
    await page.getByTestId('rebuild-taxonomy-btn').click();

    const partial = page.getByTestId('discovery-partial');
    await expect(partial).toBeVisible();
    await expect(partial).toContainText(/partial proposal/i);
    await expect(partial).toContainText(/not the full picture/i);
    // State is never colour alone.
    await expect(partial).toHaveAttribute('data-state', 'warn');
  });

  test('a failed discovery shows the error envelope and a retry — never a blank panel', async ({
    page,
  }) => {
    await stubApp(page, {
      discoverError: { status: 502, code: 'provider_error', message: 'The model did not answer.' },
    });
    await openApp(page);
    await openSettings(page);
    await page.getByTestId('rebuild-taxonomy-btn').click();

    const error = page.getByTestId('error-state');
    await expect(error).toBeVisible();
    await expect(error).toContainText(/provider_error|did not answer/i);
    await expect(page.getByRole('button', { name: /retry/i }).first()).toBeVisible();
    await expect(page.getByTestId('proposal-list')).toHaveCount(0);
  });

  test('a proposed never-miss category can never be set to archive, and says why', async ({
    page,
  }) => {
    await stubApp(page);
    await openApp(page);
    await openSettings(page);
    await page.getByTestId('rebuild-taxonomy-btn').click();

    const urgentRow = page.locator('[data-testid="proposal-row"][data-key="urgent"]');
    await expect(urgentRow).toHaveCount(1);
    await expect(urgentRow.getByTestId('proposal-never-archive-note')).toHaveText(
      'kept for you — archived under its own label, never swept as a category.',
    );
    const option = urgentRow.getByTestId('proposal-action-select').locator('option[value="archive"]');
    await expect(option).toBeDisabled();
  });
});
