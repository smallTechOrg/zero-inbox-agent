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
      'NO MAILBOX CONNECTED — click "Connect Gmail" at http://localhost:8001/app/ and re-run, ' +
        'or export ZI_SESSION=<zi_session cookie value> to seed the session headlessly ' +
        '(see tests/e2e/helpers.ts). This journey is unverified, not passing.',
    );
  });

  test('launching a run shows the progress bar and populates the clustered queue', async ({
    page,
  }) => {
    // D14/D-phase2-B/D-phase2-C: this is the one test in the file that actually
    // triggers a fresh live triage run over the default 200-thread limit and waits
    // for it to land — every other test in this file reuses that same completed run
    // via GET /api/triage/clusters defaulting to "latest". The global 60s project
    // timeout is deliberately left sane for the rest of the suite; only this real
    // end-to-end test gets more room.
    //
    // Clusters are NOT streamed progressively — persist_decisions (src/graph/
    // persistence.py) writes items, clusters, decisions and llm_calls atomically in
    // one shot at the very end of the graph, after the never-miss second-pass
    // reviewer has already run over every archive proposal. That is intentional:
    // nothing should ever be shown to the user as "archived" before the reviewer has
    // had a chance to flip a false negative back to keep. So this test must wait for
    // the run to reach a terminal state, not for clusters to trickle in mid-run.
    //
    // Timing varies with how many items the reviewer has to re-check (extra real LLM
    // calls per archived item, not just per batch). Two real measurements on this
    // live account: 209s and 429s for the same 200-thread limit. 600s budgets ~1.4x
    // over the slower observed run.
    test.setTimeout(600_000);

    const runButton = page
      .getByRole('button', { name: /Run triage/i })
      .or(page.locator('[data-testid="run-triage"]'))
      .first();
    await expect(runButton).toBeEnabled();

    await runButton.click();

    // Progress is visible immediately; it does not imply clusters exist yet.
    const progress = byTestIdOrText(page, 'run-progress', /\d+\s*\/\s*\d+/);
    await expect(progress).toBeVisible({ timeout: 30_000 });

    // Clusters only exist once the run has actually finished (see comment above).
    const clusters = await clusterRows(page);
    await expect(clusters.first()).toBeVisible({ timeout: 570_000 });
    expect(await clusters.count()).toBeGreaterThan(0);

    // A cluster row carries its count and a suggested action.
    await expect(clusters.first()).toContainText(/\d+/);
  });

  test('expanding a cluster and a thread reveals category, confidence, reasoning and tier badge', async ({
    page,
  }) => {
    // Reuses the already-completed run from the previous test (GET /api/triage/clusters
    // defaults to "latest"), so this should resolve in low single-digit seconds — the
    // wait stays inside the file's default 60s test timeout, unlike the fresh-run test
    // above which calls test.setTimeout() to get real room. A 180s inner wait here with
    // no matching outer bump was structurally unwinnable (always killed by the 60s
    // default first); found via a full-suite failure after that fresh-run test.
    const clusters = await clusterRows(page);
    await expect(clusters.first()).toBeVisible({ timeout: 30_000 });

    await clusters.first().click();

    const threads = page
      .locator('[data-testid="thread-row"]')
      .or(page.getByRole('listitem').filter({ hasText: TIER_BADGE }));
    await expect(threads.first()).toBeVisible({ timeout: 30_000 });

    // Tier badge is text, not colour alone — assert against the badge element itself
    // (data-testid="tier-badge"), not the row's concatenated textContent, since
    // adjacent DOM text nodes render without whitespace between them and break \b
    // word-boundary matching.
    const tierBadge = threads.first().locator('[data-testid="tier-badge"]');
    await expect(tierBadge).toBeVisible();
    await expect(tierBadge).toContainText(TIER_BADGE);

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
    await expect(clusters.first()).toBeVisible({ timeout: 30_000 });

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
    await expect(clusters.first()).toBeVisible({ timeout: 30_000 });

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
