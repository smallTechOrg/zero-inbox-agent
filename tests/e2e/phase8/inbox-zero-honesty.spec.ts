import { expect, test } from '@playwright/test';
import { APP_URL } from '../helpers';
import { ledgerPayload, RUN_ID, stubApp } from './fixtures';

/**
 * Screens 16 and 24 — the Inbox-Zero card must lead with the truth, and a
 * completed run holding `review_failed` rows must be recoverable.
 *
 * `distance_to_zero` is a narrow number (archives decided but not applied).
 * `distance_to_zero: 0` while threads remain is the single most dangerous
 * misreading in the product, so it is asserted directly.
 *
 * Every route is intercepted: no reviewer runs, no Gmail call is made.
 */

test.describe('Phase 8 — Inbox-Zero card honesty', () => {
  test('distance_to_zero: 0 with threads remaining is never rendered as an empty inbox', async ({
    page,
  }) => {
    const ledger = ledgerPayload({ distance_to_zero: 0 });
    expect(ledger.distance_to_zero).toBe(0);
    expect(ledger.inbox_remaining).toBeGreaterThan(0);

    await stubApp(page, { run: 'completed', ledger: { distance_to_zero: 0 } });
    await page.goto(APP_URL, { waitUntil: 'domcontentloaded' });

    const card = page.getByTestId('inbox-zero-card');
    await expect(card).toBeVisible();

    // The headline leads with the REAL remaining count.
    const headline = page.getByTestId('iz-headline');
    await expect(headline).toContainText(ledger.inbox_remaining.toLocaleString());
    await expect(headline).toContainText('still in your inbox');
    await expect(headline).not.toContainText('Inbox zero.');
    await expect(card).not.toHaveText(/Nothing is left in your inbox/);

    // …and explains that the remainder is deliberate, not forgotten.
    await expect(page.getByTestId('iz-headline-explainer')).toContainText(
      'it is holding all',
    );
    await expect(page.getByTestId('iz-headline-explainer')).toContainText('Here is exactly why:');
  });

  test('the remainder ledger names every bucket and the plain-language reason', async ({
    page,
  }) => {
    await stubApp(page, { run: 'completed', ledger: { distance_to_zero: 0 } });
    await page.goto(APP_URL, { waitUntil: 'domcontentloaded' });

    await expect(page.getByTestId('remainder-ledger-heading')).toHaveText(
      'What is still in your inbox, and why',
    );

    for (const bucket of ['needs_your_call', 'category_keep', 'held_by_never_miss', 'below_threshold']) {
      await expect(
        page.getByTestId(`remainder-row-${bucket}`),
        `bucket ${bucket} is missing from the ledger`,
      ).toHaveCount(1);
    }

    // A held bucket carries its reason in the user's terms, not the raw key.
    await expect(page.getByTestId('remainder-row-needs_your_call')).toContainText(
      'waiting for your call',
    );
    await expect(page.getByTestId('remainder-why-needs_your_call')).toContainText(
      'asking you instead of guessing',
    );
    await expect(page.getByTestId('remainder-row-category_keep')).toContainText(
      'kept by your categories',
    );

    // A zero bucket stays visible and says "none" in WORDS — not grey alone.
    const empty = page.getByTestId('remainder-row-held_by_never_miss');
    await expect(empty).toHaveAttribute('data-count', '0');
    await expect(empty).toContainText('none');

    // The definition is always visible, and reads the live taxonomy.
    await expect(page.getByTestId('inbox-zero-definition')).toContainText(
      'Inbox zero means your inbox holds only what needs a human.',
    );
    await expect(page.getByTestId('iz-keep-categories')).toContainText(
      'People, Urgent and Legal always stay',
    );
  });

  test('an inbox that is genuinely empty is the only state allowed to claim zero', async ({
    page,
  }) => {
    await stubApp(page, {
      run: 'completed',
      ledger: {
        distance_to_zero: 0,
        remainder: {
          needs_your_call: 0,
          category_keep: 0,
          held_by_never_miss: 0,
          below_threshold: 0,
          unclassified: 0,
        },
      },
    });
    await page.goto(APP_URL, { waitUntil: 'domcontentloaded' });

    await expect(page.getByTestId('iz-headline')).toContainText('Inbox zero.');
    await expect(page.getByTestId('remainder-ledger-heading')).toHaveText('Nothing is being held.');
  });

  test('a dry run that archived nothing says DRY RUN, not "could not archive"', async ({ page }) => {
    await stubApp(page, {
      run: 'completed',
      dryRun: true,
      ledger: { dry_run: true, apply_ok: false, applied: 0, distance_to_zero: 44 },
    });
    await page.goto(APP_URL, { waitUntil: 'domcontentloaded' });

    await expect(page.getByTestId('iz-dry-run-chip')).toContainText(
      'DRY RUN — nothing was archived',
    );
    await expect(page.getByTestId('apply-failed-bar')).toHaveCount(0);
  });
});

test.describe('Phase 8 — retry review (screen 24)', () => {
  test('a completed run holding unreviewed rows offers a working Retry review', async ({
    page,
  }) => {
    const log = await stubApp(page, {
      run: 'completed',
      dryRun: false,
      ledger: { not_reviewed: 120, distance_to_zero: 37, apply_ok: true },
      ledgerAfterRetry: { not_reviewed: 0, distance_to_zero: 37, apply_ok: true },
    });
    await page.goto(APP_URL, { waitUntil: 'domcontentloaded' });

    const bar = page.getByTestId('not-reviewed-bar');
    await expect(
      bar,
      'no unreviewed bar for a run whose ledger reports not_reviewed = 120',
    ).toBeVisible();
    await expect(bar).toContainText('120 threads never got past the reviewer');
    await expect(bar).toContainText('left in your inbox rather than archived unseen');
    // State is text, not colour: the bar declares its own state attribute AND says it.
    await expect(bar).toHaveAttribute('data-state', 'warn');

    const button = page.getByTestId('retry-review');
    await expect(button).toBeEnabled();
    await button.click();

    await expect
      .poll(() => log.retryReviewCalls, {
        message: 'Retry review did not call POST /api/runs/{id}/retry-review',
      })
      .toEqual([RUN_ID]);
    expect(log.requests).toContain(`POST /api/runs/${RUN_ID}/retry-review`);

    // On success the ledger is refetched and the bar clears — never left stale.
    await expect(page.getByTestId('not-reviewed-bar')).toHaveCount(0);
  });

  test('the amber unreviewed bar sits BELOW the red apply-failure bar, never instead of it', async ({
    page,
  }) => {
    await stubApp(page, {
      run: 'completed',
      dryRun: false,
      ledger: {
        not_reviewed: 61,
        distance_to_zero: 88,
        apply_ok: false,
        apply_failed_reason: 'Gmail returned 429 for 88 archives — see the action log.',
      },
    });
    await page.goto(APP_URL, { waitUntil: 'domcontentloaded' });

    const failed = page.getByTestId('apply-failed-bar');
    const unreviewed = page.getByTestId('not-reviewed-bar');
    await expect(failed).toBeVisible();
    await expect(unreviewed).toBeVisible();
    await expect(page.getByTestId('apply-failed-reason')).toContainText('Gmail returned 429');
    await expect(page.getByTestId('retry-archiving')).toBeEnabled();
    await expect(page.getByTestId('retry-review')).toBeEnabled();

    const order = await page.evaluate(() => {
      const a = document.querySelector('[data-testid="apply-failed-bar"]')!;
      const b = document.querySelector('[data-testid="not-reviewed-bar"]')!;
      // 4 = DOCUMENT_POSITION_FOLLOWING
      return (a.compareDocumentPosition(b) & 4) !== 0;
    });
    expect(order, 'the unreviewed bar must render below the apply-failure bar').toBe(true);
  });

  test('a run with nothing unreviewed shows no bar at all', async ({ page }) => {
    await stubApp(page, { run: 'completed', dryRun: false, ledger: { not_reviewed: 0 } });
    await page.goto(APP_URL, { waitUntil: 'domcontentloaded' });

    await expect(page.getByTestId('inbox-zero-card')).toBeVisible();
    await expect(page.getByTestId('not-reviewed-bar')).toHaveCount(0);
    await expect(page.getByTestId('retry-review')).toHaveCount(0);
  });
});
