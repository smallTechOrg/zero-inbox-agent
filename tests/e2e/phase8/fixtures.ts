import type { Page, Route } from '@playwright/test';

/**
 * Deterministic API fixtures for the Phase 8 surfaces.
 *
 * ⚠️  PRODUCTION SAFETY. `zero_inbox.db` holds ~12,500 real decisions for two
 * real accounts and a live Gmail account is connected. These specs therefore
 * assert **rendering and wiring contracts** through route interception: every
 * `/api/**` and `/auth/**` call the signed-in specs make is fulfilled in the
 * browser and never reaches the server. Nothing here can start a triage run,
 * archive a real message, revoke a real session or delete a real account.
 *
 * The one spec that runs fully live is `front-door.spec.ts`: a signed-out
 * visitor hitting the real `/app/` and the real `GET /api/me` (401). That is
 * read-only and is the whole point of that spec.
 *
 * Shapes are copied from `spec/api.md` (§ Phase 7, § Phase 8) and from the
 * fields the components actually read.
 */

const ok = (data: unknown) => ({
  status: 200,
  contentType: 'application/json',
  body: JSON.stringify({ data, error: null }),
});

export const RUN_ID = 'a8000000-0000-0000-0000-000000000008';
export const USER_EMAIL = 'phase8@example.com';

export const CATEGORIES = [
  { id: 'k1', key: 'people', name: 'People', description: null, channel_label_name: null, channel_label_id: null, default_action: 'keep', is_default: true, sort_order: 0, auto_act_threshold: null },
  { id: 'k2', key: 'urgent', name: 'Urgent', description: null, channel_label_name: null, channel_label_id: null, default_action: 'keep', is_default: true, sort_order: 1, auto_act_threshold: null },
  { id: 'k3', key: 'legal', name: 'Legal', description: null, channel_label_name: null, channel_label_id: null, default_action: 'keep', is_default: false, sort_order: 2, auto_act_threshold: null },
  { id: 'k4', key: 'newsletters', name: 'Newsletters', description: null, channel_label_name: null, channel_label_id: null, default_action: 'archive', is_default: true, sort_order: 3, auto_act_threshold: null },
];

export type LedgerOverrides = {
  applied?: number;
  distance_to_zero?: number;
  apply_ok?: boolean;
  apply_failed_reason?: string | null;
  dry_run?: boolean;
  not_reviewed?: number;
  remainder?: Partial<{
    needs_your_call: number;
    category_keep: number;
    held_by_never_miss: number;
    below_threshold: number;
    unclassified: number;
  }>;
};

export function ledgerPayload(over: LedgerOverrides = {}) {
  const remainder = {
    needs_your_call: 118,
    category_keep: 402,
    held_by_never_miss: 0,
    below_threshold: 63,
    unclassified: 0,
    ...(over.remainder ?? {}),
  };
  const distance = over.distance_to_zero ?? 0;
  return {
    run_id: RUN_ID,
    inbox_remaining:
      remainder.needs_your_call +
      remainder.category_keep +
      remainder.held_by_never_miss +
      remainder.below_threshold +
      remainder.unclassified +
      distance,
    distance_to_zero: distance,
    applied: over.applied ?? 311,
    apply_ok: over.apply_ok ?? true,
    apply_failed_reason: over.apply_failed_reason ?? null,
    dry_run: over.dry_run ?? false,
    not_reviewed: over.not_reviewed ?? 0,
    remainder,
    failures: [],
  };
}

/** Every request the stub served, so a spec can prove what the UI did and did not call. */
export type CallLog = {
  /** `${method} ${pathname}` for every intercepted request. */
  requests: string[];
  /** JSON bodies of every PATCH /api/settings. */
  settingsPatches: Record<string, unknown>[];
  /** run ids passed to POST /api/runs/{id}/retry-review. */
  retryReviewCalls: string[];
};

export type StubOptions = {
  /** `false` ⇒ no mailbox connected (onboarding step 1). */
  connected?: boolean;
  /** `null` ⇒ no run in history at all (a brand-new user → onboarding). */
  run?: 'completed' | null;
  dryRun?: boolean;
  ledger?: LedgerOverrides;
  /** After the first successful retry-review the ledger switches to this one. */
  ledgerAfterRetry?: LedgerOverrides;
  /** Number of `user_sessions` rows returned by GET /api/account. */
  sessions?: number;
  /** When true, `/api/me` answers 401 after `POST /auth/logout`. */
  logoutSignsOut?: boolean;
};

/**
 * Serve a complete signed-in app. Returns the call log.
 *
 * Everything is intercepted — including `/auth/logout` and `/api/events` — so
 * the specs never touch the supervised server's state.
 */
export async function stubApp(page: Page, opts: StubOptions = {}): Promise<CallLog> {
  const log: CallLog = { requests: [], settingsPatches: [], retryReviewCalls: [] };

  const settings = {
    auto_act_threshold: 0.8,
    confidence_floor: 0.75,
    dry_run: opts.dryRun ?? true,
    llm_model: 'nvidia/nemotron-3-nano-30b-a3b',
    digest_hour_local: 8,
    timezone: 'Asia/Kolkata',
  };
  let signedOut = false;
  let retried = false;

  const connections = opts.connected === false
    ? []
    : [
        {
          id: 'conn-1',
          channel: 'gmail',
          account_email: USER_EMAIL,
          status: 'connected',
          connected_at: '2026-07-01T09:00:00Z',
          last_synced_at: '2026-08-15T08:00:00Z',
        },
      ];

  const run = {
    id: RUN_ID,
    status: 'completed',
    dry_run: settings.dry_run,
    items_total: 894,
    items_decided: 894,
    counts: { applied: 311 },
    cost: { tokens_in: 0, tokens_out: 0, usd: 0 },
    error_message: null,
    started_at: '2026-08-15T08:00:00Z',
    finished_at: '2026-08-15T08:12:00Z',
    distance_to_zero: opts.ledger?.distance_to_zero ?? 0,
    apply_ok: opts.ledger?.apply_ok ?? true,
  };

  const sessionRows = Array.from({ length: opts.sessions ?? 2 }, (_, i) => ({
    id: `sess-${i + 1}`,
    created_at: '2026-08-10T10:00:00Z',
    last_seen_at: '2026-08-15T08:55:00Z',
    user_agent_summary: i === 0 ? 'Chrome on macOS' : 'Safari on iPhone',
    current: i === 0,
  }));

  await page.route('**/auth/**', async (route: Route) => {
    const url = new URL(route.request().url());
    log.requests.push(`${route.request().method()} ${url.pathname}`);
    if (url.pathname === '/auth/logout') {
      if (opts.logoutSignsOut) signedOut = true;
      return route.fulfill(ok({ signed_out: true }));
    }
    // Never let the browser walk off to Google in a test.
    return route.fulfill({ status: 200, contentType: 'text/html', body: '<p>oauth stub</p>' });
  });

  await page.route('**/api/**', async (route: Route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    const method = route.request().method();
    log.requests.push(`${method} ${path}`);

    if (path === '/api/me') {
      if (signedOut) {
        return route.fulfill({
          status: 401,
          contentType: 'application/json',
          body: JSON.stringify({
            data: null,
            error: { code: 'unauthenticated', message: 'Sign in with Google to continue.' },
          }),
        });
      }
      return route.fulfill(ok({
        user: { id: 'u1', email: USER_EMAIL, display_name: 'Phase Eight' },
        connections,
        settings,
      }));
    }

    if (path === '/api/settings' && method === 'PATCH') {
      const body = (route.request().postDataJSON() ?? {}) as Record<string, unknown>;
      log.settingsPatches.push(body);
      if (typeof body.dry_run === 'boolean') settings.dry_run = body.dry_run;
      return route.fulfill(ok({ ...settings }));
    }
    if (path === '/api/settings') return route.fulfill(ok({ ...settings }));

    if (path === '/api/runs/latest') {
      return route.fulfill(ok(opts.run === null ? null : run));
    }
    if (path === `/api/runs/${RUN_ID}`) return route.fulfill(ok(run));

    if (path === `/api/runs/${RUN_ID}/remainder`) {
      const over = retried && opts.ledgerAfterRetry ? opts.ledgerAfterRetry : opts.ledger;
      return route.fulfill(ok(ledgerPayload(over)));
    }

    if (path === `/api/runs/${RUN_ID}/retry-review` && method === 'POST') {
      log.retryReviewCalls.push(RUN_ID);
      retried = true;
      return route.fulfill(ok({ run_id: RUN_ID, queued: true, not_reviewed: 0 }));
    }

    if (path === `/api/runs/${RUN_ID}/summary`) return route.fulfill(ok({
      run_id: RUN_ID,
      status: 'completed',
      total_threads: 894,
      categories: [],
      top_clusters: [],
      needs_your_call_count: 118,
      cost_usd: 0,
      completed_at: run.finished_at,
      applied_count: 311,
      distance_to_zero: run.distance_to_zero,
    }));

    if (path === '/api/account') {
      return route.fulfill(ok({
        user: {
          id: 'u1',
          email: USER_EMAIL,
          display_name: 'Phase Eight',
          created_at: '2026-06-01T09:00:00Z',
        },
        connections,
        sessions: sessionRows,
        counts: { decisions: 894, action_logs: 311, connections: connections.length },
      }));
    }

    if (path === '/api/categories') return route.fulfill(ok(CATEGORIES));
    if (path === '/api/inbox-summary') {
      return route.fulfill(ok({ inbox_total: 583, needs_your_call: 118, categories: [] }));
    }
    if (path === '/api/triage/clusters') return route.fulfill(ok([]));
    if (path === '/api/vip') return route.fulfill(ok([]));
    if (path === '/api/profile') return route.fulfill(ok({ text: '', updated_at: null }));
    if (path === '/api/events') {
      return route.fulfill({ status: 200, contentType: 'text/event-stream', body: '' });
    }

    return route.fulfill(ok(null));
  });

  return log;
}

/**
 * Record every `new EventSource(...)` the page constructs.
 *
 * A signed-out marketing page that opens `/api/events` would 401 and then
 * reconnect-loop forever, so "did it construct one at all" is the assertion —
 * not "did a request appear", which a browser can defer.
 */
export async function recordEventSources(page: Page): Promise<void> {
  await page.addInitScript(() => {
    (window as unknown as { __esUrls: string[] }).__esUrls = [];
    const Original = window.EventSource;
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const Patched: any = function (this: unknown, url: string, init?: EventSourceInit) {
      (window as unknown as { __esUrls: string[] }).__esUrls.push(String(url));
      return new Original(url, init);
    };
    Patched.prototype = Original.prototype;
    Patched.CONNECTING = 0;
    Patched.OPEN = 1;
    Patched.CLOSED = 2;
    window.EventSource = Patched;
  });
}

export async function eventSourceUrls(page: Page): Promise<string[]> {
  return page.evaluate(() => (window as unknown as { __esUrls?: string[] }).__esUrls ?? []);
}

/**
 * Every element that conveys a state must resolve to non-empty accessible text
 * (spec/ui.md § Colour tokens — "state is never colour alone").
 *
 * Returns the outerHTML of any *visible* offender.
 */
export async function statelessColourOffenders(page: Page): Promise<string[]> {
  return page.evaluate(() => {
    const selector = [
      '[role="status"]',
      '[role="alert"]',
      '[data-state]',
      '[data-testid="run-status-pill"]',
      '[data-testid="connection-status"]',
      '[data-testid="session-current"]',
      '[data-testid="tier-badge"]',
      '[data-testid="iz-dry-run-chip"]',
      '[data-testid="dry-run-banner"]',
    ].join(',');
    const offenders: string[] = [];
    for (const el of Array.from(document.querySelectorAll(selector))) {
      const html = el as HTMLElement;
      if (html.getClientRects().length === 0) continue; // not rendered
      const text = (html.innerText || '').trim();
      const label = (html.getAttribute('aria-label') || '').trim();
      if (!text && !label) offenders.push(html.outerHTML.slice(0, 200));
    }
    return offenders;
  });
}

/**
 * How many pixels of content sit outside the viewport horizontally
 * (spec/ui.md § Responsive layout).
 *
 * `documentElement.scrollWidth` is NOT usable here: `globals.css` sets
 * `overflow-x: hidden`, so a 5,000px-wide child leaves the document scrollWidth
 * at exactly the viewport width and the naive check passes on any layout at
 * all (verified). We measure `body.scrollWidth` and the furthest right edge of
 * any laid-out element instead — content clipped by `overflow-x: hidden` is
 * still content the user cannot read.
 *
 * Elements the design deliberately parks off-screen (a closed drawer /
 * bottom-sheet) are excluded via `aria-hidden`/`hidden`, not by loosening the
 * threshold.
 */
export async function horizontalOverflow(page: Page): Promise<number> {
  return page.evaluate(() => {
    const viewport = window.innerWidth;
    let worst = document.body.scrollWidth;
    for (const el of Array.from(document.querySelectorAll<HTMLElement>('body *'))) {
      if (el.closest('[aria-hidden="true"], [hidden]')) continue;
      const rect = el.getBoundingClientRect();
      if (rect.width === 0 && rect.height === 0) continue;
      if (rect.right > worst) worst = rect.right;
    }
    return Math.round(worst - viewport);
  });
}
