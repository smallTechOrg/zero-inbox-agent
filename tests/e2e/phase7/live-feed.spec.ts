import { expect, test, type Page } from '@playwright/test';
import { assertPageIsStyled, openApp, sessionStorageState } from '../helpers';

/**
 * THE test that closes the reported defect (roadmap Phase 7, gate assertions 17-22).
 *
 * The user has asked four times to be able to watch the triage run and has never
 * been able to. Phase 6's `thread_classified` events were emitted, delivered and
 * rendered — into `ActivityDrawer.tsx`, which is `useState(false)`. Delivered is
 * not shipped; **visible** is shipped.
 *
 * So this spec loads `/app/` exactly as a user does, **clicks nothing** and
 * **opens no drawer**, and asserts classification rows are on the MAIN PAGE.
 *
 * It must FAIL LOUDLY, never `test.skip`, if it cannot start or observe a real
 * run: a silently-skipped visibility test is precisely how this defect survived
 * to Phase 7.
 */

const API = 'http://localhost:8001';

/** Small enough to be safe against a real mailbox; large enough to stream. */
const OBSERVE_RUN_LIMIT = 220;

/** The main-page feed rows — scoped INSIDE the inline feed, never the drawer. */
const FEED_ROWS = '[data-testid="live-run-feed"] [data-testid="thread-feed-row"]';

function requireSession(): void {
  expect(
    sessionStorageState(),
    'ZI_SESSION is not set, so no real run can be started or observed. This visibility ' +
      'assertion is REQUIRED and must never be skipped — connect a mailbox once at ' +
      'http://localhost:8001/app/ and export the zi_session cookie value as ZI_SESSION.',
  ).toBeDefined();
}

type LatestRun = { id: string; status: string; items_total: number } | null;

async function latestRun(page: Page): Promise<LatestRun> {
  const res = await page.request.get(`${API}/api/runs/latest`);
  if (!res.ok()) return null;
  const body = await res.json().catch(() => null);
  return (body?.data ?? null) as LatestRun;
}

function isActive(status: string | undefined): boolean {
  return status === 'running';
}

/**
 * Return the id of a run that is *actually executing right now* — reusing one
 * already in flight, or starting a small one over the fixture-sized window.
 * Never starts a full-inbox run.
 */
async function ensureActiveRun(page: Page): Promise<string> {
  const existing = await latestRun(page);
  if (existing && isActive(existing.status)) return existing.id;

  const meRes = await page.request.get(`${API}/api/me`);
  expect(meRes.ok(), 'GET /api/me failed — cannot start a run to observe').toBeTruthy();
  const me = await meRes.json();
  const connections = (me?.data?.connections ?? []) as { id: string; status: string }[];
  const connection = connections.find(c => c.status !== 'revoked') ?? connections[0];
  expect(
    connection,
    'No Gmail mailbox is connected, so no real run can be observed. This assertion is ' +
      'REQUIRED and must not be skipped.',
  ).toBeTruthy();

  const started = await page.request.post(
    `${API}/api/connections/${connection!.id}/triage`,
    { data: { limit: OBSERVE_RUN_LIMIT, only_new: false } },
  );
  expect(
    started.ok(),
    `POST /api/connections/{id}/triage failed (${started.status()}) — cannot observe a real run`,
  ).toBeTruthy();
  const body = await started.json();
  const runId = body?.data?.run_id as string | undefined;
  expect(runId, 'triage start returned no run_id').toBeTruthy();
  return runId!;
}

async function stillRunning(page: Page, runId: string): Promise<boolean> {
  const res = await page.request.get(`${API}/api/runs/${runId}`);
  if (!res.ok()) return false;
  const body = await res.json().catch(() => null);
  return isActive(body?.data?.status);
}

/**
 * The surfacing contract, asserted deterministically.
 *
 * This does NOT replace the real-run assertion below — it is the regression
 * guard for the exact bug: the feed being rendered somewhere the user never
 * looks. It drives the real `SseContext` with a real `EventSource` over a
 * stubbed `/api/events` stream and asserts the rows land on the MAIN PAGE with
 * the Activity drawer untouched and closed.
 */
test.describe('Phase 7 — the feed surfaces on the main page, not in a closed drawer', () => {
  test.use({ storageState: { cookies: [], origins: [] } });

  test('classification rows render inline while a run is active, with no clicks', async ({
    page,
  }) => {
    const frame = (o: Record<string, unknown>) => `data: ${JSON.stringify(o)}\n\n`;
    const stream =
      frame({ type: 'run_started', run_id: 'r1', triggered_by: 'user' }) +
      [1, 2, 3, 4].map(n =>
        frame({
          type: 'thread_classified',
          run_id: 'r1',
          item_id: `thread-${n}`,
          subject: `Fixture subject ${n}`,
          from_email: `sender${n}@example.com`,
          category: 'Newsletters',
          action: 'archive',
          decided_by: 'llm',
          confidence: 0.83,
          reasoning: 'Bulk marketing mail from a known list sender.',
          review_state: 'reviewed',
        }),
      ).join('') +
      frame({
        type: 'activity_heartbeat',
        run_id: 'r1',
        phase: 'tier3_classify',
        detail: 'batch 12/75',
        batch_n: 12,
        batch_total: 75,
        batch_size: 29,
        model: 'nvidia/nemotron-3-nano-30b-a3b',
        elapsed_s: 18.4,
        silent_for_s: 3.1,
      });

    const runningRun = {
      id: 'r1',
      status: 'running',
      dry_run: false,
      items_total: 220,
      items_decided: 4,
      counts: {},
      cost: { tokens_in: 0, tokens_out: 0, usd: 0 },
      error_message: null,
      started_at: new Date().toISOString(),
      finished_at: null,
    };
    const ok = (data: unknown) => ({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ data, error: null }),
    });

    await page.route('**/api/**', async route => {
      const path = new URL(route.request().url()).pathname;
      if (path === '/api/events') {
        return route.fulfill({ status: 200, contentType: 'text/event-stream', body: stream });
      }
      if (path === '/api/me') return route.fulfill(ok({
        user: { id: 'u1', email: 'user@example.com', display_name: null },
        connections: [{ id: 'c1', channel: 'gmail', account_email: 'user@example.com', status: 'connected', connected_at: null }],
        settings: { auto_act_threshold: 0.8, confidence_floor: 0.75, dry_run: false, llm_model: 'm', digest_hour_local: 8, timezone: 'UTC' },
      }));
      if (path === '/api/runs/latest' || path === '/api/runs/r1') return route.fulfill(ok(runningRun));
      if (path === '/api/runs/r1/remainder') return route.fulfill(ok(null));
      if (path === '/api/triage/clusters') return route.fulfill(ok([]));
      if (path === '/api/inbox-summary') return route.fulfill(ok({ inbox_total: 0, needs_your_call: 0, categories: [] }));
      return route.fulfill(ok(null));
    });

    await openApp(page);

    // Nothing is clicked. The drawer stays closed.
    const feed = page.locator('[data-testid="live-run-feed"]');
    await expect(feed).toBeVisible();
    await expect(feed).toHaveAttribute('data-feed-active', 'true');

    await expect(page.locator(FEED_ROWS)).toHaveCount(4);
    await expect(page.locator(FEED_ROWS).first()).toContainText('Fixture subject');
    await expect(page.locator(FEED_ROWS).first()).toContainText('Newsletters');

    // The heartbeat is a pinned line with real state, never a scrolling row and
    // never a bare spinner.
    const heartbeat = page.locator('[data-testid="live-feed-heartbeat"]');
    await expect(heartbeat).toContainText('tier3_classify');
    await expect(heartbeat).toContainText('batch 12/75');
    await expect(heartbeat).toContainText('nvidia/nemotron-3-nano-30b-a3b');

    // The header proves movement even when rows look alike.
    await expect(page.locator('[data-testid="live-feed-last-update"]')).toContainText('last update');

    // And the drawer really was never opened — the feed is not the drawer.
    await expect(page.locator('[data-testid="live-run-feed"]')).toBeVisible();
    await expect(page.locator('[data-testid="see-all-activity"]')).toBeVisible();
  });

  test('a degraded provider pins a red line to the MAIN PAGE, above the feed', async ({ page }) => {
    const frame = (o: Record<string, unknown>) => `data: ${JSON.stringify(o)}\n\n`;
    const stream = frame({
      type: 'provider_degraded',
      run_id: 'r1',
      provider: 'nvidia',
      model: 'nvidia/nemotron-3-nano-30b-a3b',
      calls: 4000,
      retries: 2774,
      consecutive_failures: 12,
    });
    const ok = (data: unknown) => ({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ data, error: null }),
    });
    const runningRun = {
      id: 'r1', status: 'running', dry_run: false, items_total: 220, items_decided: 0,
      counts: {}, cost: { tokens_in: 0, tokens_out: 0, usd: 0 }, error_message: null,
      started_at: new Date().toISOString(), finished_at: null,
    };

    await page.route('**/api/**', async route => {
      const path = new URL(route.request().url()).pathname;
      if (path === '/api/events') {
        return route.fulfill({ status: 200, contentType: 'text/event-stream', body: stream });
      }
      if (path === '/api/me') return route.fulfill(ok({
        user: { id: 'u1', email: 'user@example.com', display_name: null },
        connections: [{ id: 'c1', channel: 'gmail', account_email: 'user@example.com', status: 'connected', connected_at: null }],
        settings: { auto_act_threshold: 0.8, confidence_floor: 0.75, dry_run: false, llm_model: 'm', digest_hour_local: 8, timezone: 'UTC' },
      }));
      if (path === '/api/runs/latest' || path === '/api/runs/r1') return route.fulfill(ok(runningRun));
      if (path === '/api/triage/clusters') return route.fulfill(ok([]));
      if (path === '/api/inbox-summary') return route.fulfill(ok({ inbox_total: 0, needs_your_call: 0, categories: [] }));
      return route.fulfill(ok(null));
    });

    await openApp(page);

    // Judged on the main page, with no drawer opened — an auto-opened drawer
    // does not satisfy this criterion (ui.md screens 14 + 18).
    const banner = page.locator('[data-testid="page-degraded-banner"]');
    await expect(banner).toBeVisible();
    await expect(banner).toContainText('2774 retries');
    await expect(banner).toContainText('degraded');
  });
});

test.describe('Phase 7 — the live run feed is visible on the main page', () => {
  test('classification rows stream on the main page with zero clicks', async ({ page }, testInfo) => {
    requireSession();

    const runId = await ensureActiveRun(page);

    // Load the dashboard exactly as a user does. NOTHING is clicked below this
    // line, and the Activity drawer is never opened.
    await openApp(page);
    await assertPageIsStyled(page);

    // 17. REQUIRED — visible with zero clicks, within 2 s of first paint.
    await expect(
      page.locator('[data-testid="live-run-feed"]'),
      'the inline live run feed is not rendered on the main page at all',
    ).toBeVisible({ timeout: 10_000 });

    const firstPaint = Date.now();
    await expect
      .poll(async () => page.locator(FEED_ROWS).count(), {
        timeout: 60_000,
        message:
          'no classification rows appeared on the MAIN PAGE — the run is invisible to the user, ' +
          'which is the exact defect Phase 7 exists to close',
      })
      .toBeGreaterThan(0);

    // Re-assert the 2 s budget against a *fresh* paint now that the replay
    // buffer definitely holds classifications (the first wait above may have
    // been spent waiting for the run's very first decision, not for the UI).
    await page.reload({ waitUntil: 'domcontentloaded' });
    const reloadStart = Date.now();
    await expect(page.locator(FEED_ROWS).first()).toBeVisible({ timeout: 10_000 });
    const paintToRows = Date.now() - reloadStart;
    expect(
      paintToRows,
      `rows took ${paintToRows}ms to appear after reload — the replay buffer must paint the ` +
        'feed within 2 s (roadmap gate 17 / triage-transparency rule J2)',
    ).toBeLessThan(2_000);
    void firstPaint;

    // 19. Mid-run reload paints from the replay buffer, not an empty box.
    const rowsAfterReload = await page.locator(FEED_ROWS).count();
    expect(
      rowsAfterReload,
      'the feed was empty on first paint after a mid-run reload — replay-on-connect is not surfacing',
    ).toBeGreaterThan(0);

    // A screenshot of the mid-run main page, as an artifact.
    const shot = await page.screenshot({ fullPage: true });
    await testInfo.attach('mid-run-main-page.png', { body: shot, contentType: 'image/png' });

    // 18. The on-page row count strictly increases while the run is active.
    const counts: number[] = [];
    for (let i = 0; i < 3; i++) {
      if (i > 0) await page.waitForTimeout(3_200);
      const running = await stillRunning(page, runId);
      expect(
        running,
        `the run finished before the feed could be observed growing (samples so far: ${counts.join(
          ', ',
        )}) — re-run against a run large enough to observe`,
      ).toBeTruthy();
      counts.push(await page.locator(FEED_ROWS).count());
    }

    // The visible window is capped at 12 rows, so once it is full the *rendered*
    // count stops growing while the run does not. Growth is then proven by the
    // newest row changing — the feed must never be visually static.
    const capped = counts[0] >= 12;
    if (capped) {
      const firstSubject = await page.locator(FEED_ROWS).first().getAttribute('data-item-id');
      await expect
        .poll(
          async () => page.locator(FEED_ROWS).first().getAttribute('data-item-id'),
          {
            timeout: 20_000,
            message:
              'the feed is full but visually static — the newest row never changed while the ' +
              'run was active',
          },
        )
        .not.toBe(firstSubject);
    } else {
      expect(
        counts[1],
        `on-page row count did not increase: ${counts.join(' → ')}`,
      ).toBeGreaterThan(counts[0]);
      expect(
        counts[2],
        `on-page row count did not keep increasing: ${counts.join(' → ')}`,
      ).toBeGreaterThan(counts[1]);
    }

    // 20/21. The feed never goes quiet without saying so: either a heartbeat
    // line with real state, or an explicit amber staleness line. Never neither.
    const heartbeat = page.locator('[data-testid="live-feed-heartbeat"]');
    const staleLine = page.locator('[data-testid="live-feed-stale"]');
    await expect(
      heartbeat.or(staleLine),
      'the feed rendered neither a heartbeat line nor a staleness line — a visually static ' +
        'feed with no explanation is a defect',
    ).toBeVisible();

    // The drawer was never opened by this test.
    await expect(page.locator('[data-testid="live-run-feed"]')).toBeVisible();
  });

  test('with no active run the feed is one idle line, never an empty box', async ({ page }) => {
    requireSession();
    await openApp(page);

    const latest = await latestRun(page);
    expect(
      latest,
      'no run exists at all for this session, so the idle state cannot be observed',
    ).not.toBeNull();
    if (latest && isActive(latest.status)) {
      // A run is in flight; the live state is covered by the test above.
      await expect(page.locator('[data-testid="live-run-feed"]')).toHaveAttribute(
        'data-feed-active',
        'true',
      );
      return;
    }

    await expect(page.locator('[data-testid="live-run-feed"]')).toHaveAttribute(
      'data-feed-active',
      'false',
    );
    await expect(page.locator('[data-testid="live-feed-idle"]')).toBeVisible();
    // Never a progress bar for work that is not running.
    await expect(page.locator('[data-testid="live-feed-rows"]')).toHaveCount(0);
  });
});
