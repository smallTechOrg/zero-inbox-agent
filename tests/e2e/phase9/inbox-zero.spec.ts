import { expect, test } from '@playwright/test';
import { clearReorgJob, openApp, stubApp } from './fixtures';

/**
 * Phase 9, user-test steps 5, 6 and 8 (spec/roadmap.md § How the user tests it),
 * screen 28 (spec/ui.md).
 *
 * The Inbox-Zero card reads **0 still in your inbox** — not 365 — and states,
 * in plain words, that never-miss mail was archived **under its own label, not
 * deleted, and is one click away**. "Inbox zero" renders only when
 * `inbox_remaining === 0`; anything else renders the remainder by reason.
 *
 * Every route is intercepted: no run, no apply, no undo touches the server.
 */

test.describe('Phase 9 — the Inbox-Zero card at actual zero', () => {
  test.beforeEach(async ({ page }) => {
    await clearReorgJob(page);
  });

  test('the ledger reaches 0 and the card says so', async ({ page }) => {
    await stubApp(page, { ledger: {} });
    await openApp(page);

    const card = page.getByTestId('inbox-zero-card');
    await expect(card).toBeVisible();
    await expect(page.getByTestId('iz-headline')).toContainText('Inbox zero.');
    await expect(page.getByTestId('iz-inbox-remaining')).toContainText('0');
    await expect(page.getByTestId('iz-distance')).toContainText('0');
    // The 365 the phase exists to eliminate must be nowhere on the card.
    await expect(card).not.toContainText('365');
    await expect(page.getByTestId('remainder-ledger-heading')).toContainText(
      'Nothing is being held.',
    );
  });

  test('the card states the redefinition: archived under its own label, not deleted, one click away', async ({
    page,
  }) => {
    await stubApp(page, { ledger: {} });
    await openApp(page);

    const block = page.getByTestId('never-miss-labels');
    await expect(block).toBeVisible();
    await expect(block).toContainText(/archived under its own label/i);
    await expect(block).toContainText(/not deleted/i);
    await expect(block).toContainText(/one click away/i);
    // Trust invariant: never presented as deleted, trashed or spam-reported.
    await expect(block).toContainText(/deleted, trashed or marked as spam/i);
    // …and undo is always offered, by name.
    await expect(block).toContainText(/Undo this run/i);

    // The three never-miss labels, as real Gmail links, with their counts.
    for (const [key, label, count] of [
      ['urgent', 'ZeroInbox/Urgent', '158'],
      ['important', 'ZeroInbox/Important', '46'],
      ['people', 'ZeroInbox/People', '23'],
    ] as const) {
      const link = page.getByTestId(`never-miss-label-${key}`);
      await expect(link, `${label} is not named on the card`).toBeVisible();
      await expect(link).toContainText(label);
      await expect(link).toContainText(count);
      await expect(link).toHaveAttribute('href', /mail\.google\.com/);
    }
  });

  test('"Inbox zero" never renders while threads remain — the remainder is named by reason', async ({
    page,
  }) => {
    await stubApp(page, {
      ledger: {
        distance_to_zero: 0,
        remainder: { needs_your_call: 16, below_threshold: 46, held_by_never_miss: 227 },
      },
    });
    await openApp(page);

    const card = page.getByTestId('inbox-zero-card');
    await expect(page.getByTestId('iz-headline')).toContainText('289');
    await expect(page.getByTestId('iz-headline')).toContainText('still in your inbox');
    await expect(page.getByTestId('iz-headline')).not.toContainText('Inbox zero.');
    await expect(card).not.toContainText(/we reached zero/i);
    await expect(page.getByTestId('remainder-row-needs_your_call')).toHaveAttribute('data-count', '16');
    await expect(page.getByTestId('remainder-row-below_threshold')).toHaveAttribute('data-count', '46');
    await expect(page.getByTestId('remainder-row-held_by_never_miss')).toHaveAttribute('data-count', '227');
  });

  test('the two Phase-9 buckets are shown when non-zero and hidden when zero', async ({ page }) => {
    await stubApp(page, { ledger: {} });
    await openApp(page);
    await expect(page.getByTestId('inbox-zero-card')).toBeVisible();
    // At zero they are noise, so they are absent…
    await expect(page.getByTestId('remainder-row-no_never_miss_label')).toHaveCount(0);
    await expect(page.getByTestId('remainder-row-unreviewed_applied')).toHaveCount(0);

    // …but a thread held because no never-miss label resolved is NEVER silent,
    // and the 44 historic unreviewed-but-applied rows are stated, not hidden.
    await stubApp(page, {
      ledger: { remainder: { no_never_miss_label: 3, unreviewed_applied: 44 } },
    });
    await page.reload({ waitUntil: 'domcontentloaded' });

    const noLabel = page.getByTestId('remainder-row-no_never_miss_label');
    await expect(noLabel).toHaveAttribute('data-count', '3');
    await expect(noLabel).toContainText(/no never-miss label could be resolved/i);
    const unreviewed = page.getByTestId('remainder-row-unreviewed_applied');
    await expect(unreviewed).toHaveAttribute('data-count', '44');
    await expect(unreviewed).toContainText(/before the reviewer covered every action/i);
  });

  test('undo for the whole run is offered and enabled at zero', async ({ page }) => {
    await stubApp(page, { ledger: {} });
    await openApp(page);

    const undo = page.getByTestId('undo-run');
    await expect(undo).toBeVisible();
    await expect(undo).toBeEnabled();
    await expect(undo).toContainText(/undo this run/i);
  });
});
