import { expect, test } from '@playwright/test';
import { JOB_ID, openApp, openSettings, seedReorgJob, stubApp, type ReorgLedgerStub } from './fixtures';

/**
 * Phase 9, user-test steps 3 and 4 (spec/roadmap.md § How the user tests it),
 * screen 27 (spec/ui.md).
 *
 * The re-organisation progress card must advance **with no clicks**, list what
 * it skipped **while it runs** (by reason and count), render amber `Partial`
 * whenever anything was skipped, and offer **Undo the whole re-organisation**
 * as one button behind a confirm dialog naming the exact count.
 *
 * SAFETY: every route is intercepted. This spec never starts, cancels or undoes
 * a re-organisation on the live account, and the undo confirm dialog is
 * DISMISSED in the assertion that reaches it.
 */

const RUNNING = (done: number, skipped: Record<string, number>): ReorgLedgerStub => ({
  job_id: JOB_ID,
  status: 'running',
  total: 10336,
  done,
  skipped,
  undoable: true,
  phase: 're-classifying',
  current_category: 'Social / Facebook',
  dry_run: false,
  error_message: null,
});

test.describe('Phase 9 — re-organisation progress', () => {
  test('the card renders on the main page and advances with NO clicks', async ({ page }) => {
    const log = await stubApp(page, {
      reorgLedgers: [
        RUNNING(1200, { already_correct: 40 }),
        RUNNING(3400, { already_correct: 90, not_reviewed: 5 }),
        RUNNING(6800, { already_correct: 140, not_reviewed: 12 }),
      ],
    });
    await seedReorgJob(page);
    await openApp(page);

    const card = page.getByTestId('reorg-card');
    await expect(card).toBeVisible();
    await expect(card).toHaveAttribute('data-status', 'running');
    await expect(page.getByTestId('reorg-progress-count')).toContainText('1,200 / 10,336');

    // No interaction at all from here on — the card must move on its own.
    await expect(page.getByTestId('reorg-progress-count')).toContainText('3,400 / 10,336', {
      timeout: 20_000,
    });
    await expect(page.getByTestId('reorg-progress-count')).toContainText('6,800 / 10,336', {
      timeout: 20_000,
    });
    expect(log.reorgPolls, 'the card is not polling — it cannot self-update').toBeGreaterThan(2);

    // Working-but-slow and stuck look different: the age of the last read is on screen.
    await expect(page.getByTestId('reorg-last-update')).toContainText(/last update \d+s ago/);
    await expect(page.getByTestId('reorg-phase')).toContainText('re-classifying');
  });

  test('the skipped-by-reason table renders WHILE it runs, with every reason and its count', async ({
    page,
  }) => {
    await stubApp(page, {
      reorgLedgers: [RUNNING(1200, { already_correct: 40, not_reviewed: 12, gmail_error: 3 })],
    });
    await seedReorgJob(page);
    await openApp(page);

    // Still running — nothing about skips may wait for the end.
    await expect(page.getByTestId('reorg-card')).toHaveAttribute('data-status', 'running');

    const table = page.getByTestId('reorg-skipped-table');
    await expect(table).toBeVisible();
    // The closed set from spec/api.md § Phase 9 — every reason renders, zeroes included.
    for (const reason of [
      'not_reviewed',
      'no_category_fit',
      'gmail_error',
      'already_correct',
      'dry_run',
      'cancelled',
    ]) {
      await expect(
        page.getByTestId(`reorg-skip-${reason}`),
        `skip reason ${reason} is not rendered`,
      ).toHaveCount(1);
    }
    await expect(page.getByTestId('reorg-skip-already_correct')).toHaveAttribute('data-count', '40');
    await expect(page.getByTestId('reorg-skip-not_reviewed')).toHaveAttribute('data-count', '12');
    await expect(page.getByTestId('reorg-skip-gmail_error')).toHaveAttribute('data-count', '3');
    // A zero row says "none" in words, never grey alone.
    await expect(page.getByTestId('reorg-skip-cancelled')).toContainText('none');
    await expect(page.getByTestId('reorg-skipped-heading')).toContainText('55');

    // Cancel is offered while it runs, and promises the done work stays done.
    const cancel = page.getByTestId('reorg-cancel');
    await expect(cancel).toBeEnabled();
    await expect(cancel).toHaveAttribute('title', /stays done/i);
  });

  test('a finished job that skipped anything is amber Partial — never green, never "completed"', async ({
    page,
  }) => {
    await stubApp(page, {
      reorgLedgers: [
        {
          job_id: JOB_ID,
          status: 'completed',
          total: 10336,
          done: 10100,
          skipped: { not_reviewed: 120, no_category_fit: 116 },
          undoable: true,
          dry_run: false,
          error_message: null,
        },
      ],
    });
    await seedReorgJob(page);
    await openApp(page);

    const card = page.getByTestId('reorg-card');
    await expect(card).toBeVisible();
    await expect(card).toHaveAttribute('data-state', 'warn');
    await expect(page.getByTestId('reorg-partial-badge')).toHaveText('Partial');
    await expect(page.getByTestId('reorg-headline')).not.toContainText(/^Re-organised your past/);
    await expect(page.getByTestId('reorg-headline')).toContainText(/some threads were skipped/i);
    await expect(page.getByTestId('reorg-skip-not_reviewed')).toHaveAttribute('data-count', '120');

    // done + sum(skipped) == total, and the card shows both halves.
    await expect(page.getByTestId('reorg-progress-count')).toContainText('10,100 / 10,336');
    await expect(page.getByTestId('reorg-progress-pct')).toContainText('100%');
  });

  test('"Undo the whole re-organisation" is one button behind a confirm naming the exact count', async ({
    page,
  }) => {
    const log = await stubApp(page, {
      reorgLedgers: [
        {
          job_id: JOB_ID,
          status: 'completed',
          total: 10336,
          done: 9812,
          skipped: { already_correct: 524 },
          undoable: true,
          dry_run: false,
          error_message: null,
        },
      ],
    });
    await seedReorgJob(page);
    await openApp(page);

    const undo = page.getByTestId('reorg-undo');
    await expect(undo).toBeVisible();
    await expect(undo).toBeEnabled();
    await expect(undo).toHaveText('Undo the whole re-organisation');

    // First: the confirm dialog must name the count and promise nothing is deleted.
    // It is DISMISSED — this spec asserts the dialog, it does not run an undo.
    let message = '';
    page.once('dialog', async dialog => {
      message = dialog.message();
      await dialog.dismiss();
    });
    await undo.click();
    await expect.poll(() => message, { timeout: 10_000 }).toContain('9,812');
    expect(message).toMatch(/nothing is deleted/i);
    expect(message).toMatch(/running this twice is safe/i);
    expect(log.reorgUndos, 'dismissing the confirm must not call undo').toBe(0);
  });

  test('a dry-run job says Gmail is untouched, and a failed job shows its reason', async ({
    page,
  }) => {
    await stubApp(page, {
      dryRun: true,
      reorgLedgers: [
        {
          job_id: JOB_ID,
          status: 'failed',
          total: 10336,
          done: 400,
          skipped: { gmail_error: 12 },
          undoable: true,
          dry_run: true,
          error_message: 'Gmail refused the batch (rate_limited) — the job stopped and nothing was lost.',
        },
      ],
    });
    await seedReorgJob(page);
    await openApp(page);

    await expect(page.getByTestId('reorg-dry-run-chip')).toContainText(/Gmail is not being touched/i);
    await expect(page.getByTestId('reorg-card')).toHaveAttribute('data-state', 'bad');
    await expect(page.getByTestId('reorg-error-message')).toContainText('rate_limited');
  });

  test('starting a re-organisation from Settings puts the card on the main page with no further clicks', async ({
    page,
  }) => {
    const log = await stubApp(page, { reorgLedgers: [RUNNING(500, { already_correct: 4 })] });
    await openApp(page);
    await openSettings(page);
    await page.getByTestId('rebuild-taxonomy-btn').click();
    await expect(page.getByTestId('proposal-row').first()).toBeVisible();
    await page.getByTestId('approve-taxonomy-btn').click();
    await page.getByTestId('reorg-everything-btn').click();

    expect(log.reorgStarts, 'the job was not started exactly once').toHaveLength(1);

    // The user is put back on the main page with the card in front of them —
    // the progress must be visible with NO further clicks (ui.md screen 27).
    await expect(page.locator('main')).toHaveAttribute('id', 'triage-history');
    await expect(page.getByTestId('reorg-card')).toBeVisible();
    await expect(page.getByTestId('reorg-progress-count')).toContainText('500 / 10,336');
  });
});
