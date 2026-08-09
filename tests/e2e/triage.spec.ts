import { test, expect, type Page } from '@playwright/test';
import { openApp, byTestIdOrText, isMailboxConnected } from './helpers';

/**
 * Phase 1 primary journey: launch a triage run over the connected mailbox and sweep
 * the clustered queue.
 *
 * This requires a real connected Gmail account (the OAuth flow cannot be automated
 * against Google's consent screen). With no mailbox connected the whole file is
 * skipped with an explicit, loud reason — never silently "passed" as coverage.
 */

const TIER_BADGE = /\b(RULE|SENDER HISTORY|LLM|DEEP READ|REVIEWER)\b/;

async function clusterRows(page: Page) {
  const byId = page.locator('[data-testid="cluster-row"]');
  if ((await byId.count()) > 0) return byId;
  return page.getByRole('listitem').filter({ hasText: /\d+\s+threads?/i });
}

test.describe('Phase 1 — clustered triage queue', () => {
  test.beforeEach(async ({ page }) => {
    await openApp(page);
    const connected = await isMailboxConnected(page);
    test.skip(
      !connected,
      'NO MAILBOX CONNECTED — click "Connect Gmail" at http://localhost:8001/app/ and re-run. ' +
        'This journey is unverified, not passing.',
    );
  });

  test('launching a run shows the progress bar and populates the clustered queue', async ({
    page,
  }) => {
    const runButton = page
      .getByRole('button', { name: /Run triage/i })
      .or(page.locator('[data-testid="run-triage"]'))
      .first();
    await expect(runButton).toBeEnabled();

    await runButton.click();

    // Live progress while the run is in flight.
    const progress = byTestIdOrText(page, 'run-progress', /\d+\s*\/\s*\d+/);
    await expect(progress).toBeVisible({ timeout: 30_000 });

    // Results stream in: clusters appear and are usable before the run finishes.
    const clusters = await clusterRows(page);
    await expect(clusters.first()).toBeVisible({ timeout: 180_000 });
    expect(await clusters.count()).toBeGreaterThan(0);

    // A cluster row carries its count and a suggested action.
    await expect(clusters.first()).toContainText(/\d+/);
  });

  test('expanding a cluster and a thread reveals category, confidence, reasoning and tier badge', async ({
    page,
  }) => {
    const clusters = await clusterRows(page);
    await expect(clusters.first()).toBeVisible({ timeout: 180_000 });

    await clusters.first().click();

    const threads = page
      .locator('[data-testid="thread-row"]')
      .or(page.getByRole('listitem').filter({ hasText: TIER_BADGE }));
    await expect(threads.first()).toBeVisible({ timeout: 30_000 });

    // Tier badge is text, not colour alone.
    await expect(threads.first()).toContainText(TIER_BADGE);

    await threads.first().click();

    const detail = page
      .locator('[data-testid="thread-detail"]')
      .or(page.getByTestId('decision-reasoning'))
      .first();
    const reasoning = byTestIdOrText(page, 'decision-reasoning', /.+/);

    await expect(detail.or(reasoning).first()).toBeVisible({ timeout: 15_000 });

    // Full reasoning, verbatim and non-trivial.
    const reasoningText = await reasoning.innerText();
    expect(reasoningText.trim().length, 'reasoning text is empty or truncated').toBeGreaterThan(15);

    // Confidence is rendered numerically or as a labelled bar.
    const confidence = byTestIdOrText(page, 'decision-confidence', /\d+(\.\d+)?\s*%?/);
    await expect(confidence).toBeVisible();

    // Category chip.
    const category = page.locator('[data-testid="decision-category"]').first();
    if ((await category.count()) > 0) {
      await expect(category).not.toBeEmpty();
    }
  });

  test('the "Needs your call" bucket renders and is pinned above the clusters', async ({
    page,
  }) => {
    const needs = byTestIdOrText(page, 'needs-your-call', /Needs your call/i);
    await expect(needs).toBeVisible({ timeout: 60_000 });

    const clusters = await clusterRows(page);
    if ((await clusters.count()) > 0) {
      const needsBox = await needs.boundingBox();
      const clusterBox = await clusters.first().boundingBox();
      if (needsBox && clusterBox) {
        expect(
          needsBox.y,
          '"Needs your call" must be pinned above the cluster list',
        ).toBeLessThanOrEqual(clusterBox.y);
      }
    }
  });

  test('approving a cluster records the decision and does not touch Gmail', async ({ page }) => {
    const clusters = await clusterRows(page);
    await expect(clusters.first()).toBeVisible({ timeout: 180_000 });

    const approve = clusters
      .first()
      .getByRole('button', { name: /Approve all/i })
      .or(clusters.first().locator('[data-testid="approve-cluster"]'))
      .first();
    await expect(approve).toBeEnabled();

    const [response] = await Promise.all([
      page.waitForResponse(
        (r) => /\/api\/triage\/clusters\/.+\/approve/.test(r.url()) && r.request().method() === 'POST',
        { timeout: 30_000 },
      ),
      approve.click(),
    ]);

    expect(response.status(), 'approving a cluster must succeed').toBe(200);
    const body = await response.json();
    expect(body.error).toBeNull();
    expect(body.data.updated, 'approve must update at least one decision').toBeGreaterThan(0);

    // The dry-run banner is still there afterwards — nothing was applied to Gmail.
    await expect(byTestIdOrText(page, 'dry-run-banner', /DRY RUN/i)).toBeVisible();
  });

  test('rejecting a cluster is recorded as rejected', async ({ page }) => {
    const clusters = await clusterRows(page);
    await expect(clusters.first()).toBeVisible({ timeout: 180_000 });

    const target = clusters.nth((await clusters.count()) > 1 ? 1 : 0);
    const reject = target
      .getByRole('button', { name: /Reject all/i })
      .or(target.locator('[data-testid="reject-cluster"]'))
      .first();
    await expect(reject).toBeEnabled();

    const [response] = await Promise.all([
      page.waitForResponse(
        (r) => /\/api\/triage\/clusters\/.+\/approve/.test(r.url()) && r.request().method() === 'POST',
        { timeout: 30_000 },
      ),
      reject.click(),
    ]);

    expect(response.status()).toBe(200);
    expect(response.request().postDataJSON()?.status).toBe('rejected');
  });

  test('an unknown run id renders the error state, not a crash', async ({ page }) => {
    const response = await page.request.get(
      'http://localhost:8001/api/runs/00000000-0000-0000-0000-000000000000',
    );
    const body = await response.json();

    expect(response.status()).toBe(404);
    expect(body.data).toBeNull();
    expect(body.error.code).toBe('not_found');
  });
});
