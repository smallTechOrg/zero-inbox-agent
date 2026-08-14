import type { Page, Route } from '@playwright/test';

/**
 * Deterministic API fixtures for the Phase 7 card/settings specs.
 *
 * These specs assert **rendering contracts** — the verbatim inbox-zero
 * definition, every remainder bucket including the zero-count ones, and the
 * red apply-failure bar. Reproducing those states against the real mailbox
 * would mean writing to `zero_inbox.db`, which holds real mail decisions for
 * two real accounts. So the shapes below are copied from `spec/api.md`
 * § Phase 7 verbatim and served by route interception instead.
 *
 * The live-run visibility assertion (`live-feed.spec.ts`) is deliberately NOT
 * mocked — it runs against a real run.
 */

const ok = (data: unknown) => ({
  status: 200,
  contentType: 'application/json',
  body: JSON.stringify({ data, error: null }),
});

export const RUN_ID = 'fbeed060-0000-0000-0000-000000000000';

/**
 * The seeded taxonomy, served by `GET /api/categories`.
 *
 * `Receipts` is `archive` as of Phase 7 — a product-owner decision recorded in
 * `spec/capabilities/drive-to-inbox-zero.md` (with `keep` the inbox floor was
 * ~715 threads and the product could not reach its own definition of zero).
 *
 * The Inbox-Zero card interpolates BOTH category lists in its definition text
 * from this data (ui.md #16). The specs therefore derive their expectations from
 * `keepNames()` / `archiveNames()` below — never from a frozen literal — so if
 * this taxonomy changes again and the component stops interpolating, the test
 * fails. Asserting the literal is what let the card ship "Receipts always stay"
 * with a green suite.
 */
export const CATEGORIES = [
  { id: 'k1', key: 'people', name: 'People', description: null, channel_label_name: null, channel_label_id: null, default_action: 'keep', is_default: true, sort_order: 0, auto_act_threshold: null },
  { id: 'k2', key: 'urgent', name: 'Urgent', description: null, channel_label_name: null, channel_label_id: null, default_action: 'keep', is_default: true, sort_order: 1, auto_act_threshold: null },
  { id: 'k3', key: 'legal', name: 'Legal', description: null, channel_label_name: null, channel_label_id: null, default_action: 'keep', is_default: false, sort_order: 2, auto_act_threshold: null },
  { id: 'k4', key: 'receipts', name: 'Receipts', description: null, channel_label_name: null, channel_label_id: null, default_action: 'archive', is_default: true, sort_order: 3, auto_act_threshold: 0.85 },
  { id: 'k5', key: 'newsletters', name: 'Newsletters', description: null, channel_label_name: null, channel_label_id: null, default_action: 'archive', is_default: true, sort_order: 4, auto_act_threshold: null },
  { id: 'k6', key: 'outreach', name: 'Outreach', description: null, channel_label_name: null, channel_label_id: null, default_action: 'archive', is_default: true, sort_order: 5, auto_act_threshold: 0.85 },
];

const namesFor = (action: string) =>
  CATEGORIES.filter(c => c.default_action === action).map(c => c.name);

export const keepNames = () => namesFor('keep');
export const archiveNames = () => namesFor('archive');

/** "A", "A and B", "A, B and C" — must match the card's own joiner. */
export function nameList(names: string[]): string {
  if (names.length === 0) return '';
  if (names.length === 1) return names[0];
  return `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`;
}

export type RemainderOverrides = {
  applied?: number;
  distance_to_zero?: number;
  apply_ok?: boolean;
  apply_failed_reason?: string | null;
  dry_run?: boolean;
  remainder?: Partial<{
    needs_your_call: number;
    category_keep: number;
    held_by_never_miss: number;
    below_threshold: number;
    unclassified: number;
  }>;
};

export function remainderPayload(over: RemainderOverrides = {}) {
  const remainder = {
    needs_your_call: 213,
    category_keep: 1314,
    // Deliberately zero: a bucket at zero must render greyed, never disappear.
    held_by_never_miss: 0,
    below_threshold: 71,
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
    applied: over.applied ?? 544,
    apply_ok: over.apply_ok ?? true,
    apply_failed_reason: over.apply_failed_reason ?? null,
    dry_run: over.dry_run ?? false,
    remainder,
    failures: [],
  };
}

export type StubOptions = {
  remainder?: RemainderOverrides;
  autoActThreshold?: number;
  /** Records every POST /api/runs/{id}/apply the UI makes. */
  applyCalls?: string[];
};

/** Serve a complete, connected, one-completed-run dashboard. */
export async function stubDashboard(page: Page, opts: StubOptions = {}): Promise<void> {
  const settings = {
    auto_act_threshold: opts.autoActThreshold ?? 0.8,
    confidence_floor: 0.75,
    dry_run: false,
    llm_model: 'nvidia/nemotron-3-nano-30b-a3b',
    digest_hour_local: 8,
    timezone: 'Asia/Kolkata',
  };

  const run = {
    id: RUN_ID,
    status: 'completed',
    dry_run: false,
    items_total: 2176,
    items_decided: 2176,
    counts: {},
    cost: { tokens_in: 0, tokens_out: 0, usd: 0 },
    error_message: null,
    started_at: new Date(Date.now() - 600_000).toISOString(),
    finished_at: new Date(Date.now() - 60_000).toISOString(),
    distance_to_zero: opts.remainder?.distance_to_zero ?? 0,
    apply_ok: opts.remainder?.apply_ok ?? true,
  };

  await page.route('**/api/**', async (route: Route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    const method = route.request().method();

    if (path === '/api/me') return route.fulfill(ok({
      user: { id: 'u1', email: 'user@example.com', display_name: null },
      connections: [
        { id: 'c1', channel: 'gmail', account_email: 'user@example.com', status: 'connected', connected_at: null },
      ],
      settings,
    }));

    if (path === '/api/settings' && method === 'PATCH') {
      const body = route.request().postDataJSON() as { auto_act_threshold?: number };
      const value = body?.auto_act_threshold ?? settings.auto_act_threshold;
      return route.fulfill(ok({
        ...settings,
        auto_act_threshold: value,
        warning: value > 0.9 ? 'above_model_ceiling' : null,
      }));
    }

    if (path === '/api/runs/latest') return route.fulfill(ok(run));
    if (path === `/api/runs/${RUN_ID}`) return route.fulfill(ok(run));

    if (path === `/api/runs/${RUN_ID}/remainder`) {
      return route.fulfill(ok(remainderPayload(opts.remainder)));
    }

    if (path === `/api/runs/${RUN_ID}/apply` && method === 'POST') {
      opts.applyCalls?.push(RUN_ID);
      // The real route (src/api/runs.py:apply_run) backgrounds the pass and
      // returns the REMAINDER ledger as it stands, plus `queued` — not an apply
      // ledger. spec/api.md § Phase 7 now records that verified shape; the
      // fixture matches it field-for-field, because a fixture that drifts from
      // the real response is a test that proves nothing.
      return route.fulfill(ok({ ...remainderPayload(opts.remainder), queued: true }));
    }

    if (path === `/api/runs/${RUN_ID}/summary`) return route.fulfill(ok({
      run_id: RUN_ID,
      status: 'completed',
      total_threads: 2176,
      categories: [],
      top_clusters: [],
      needs_your_call_count: 213,
      cost_usd: 0,
      completed_at: run.finished_at,
      applied_count: 544,
      distance_to_zero: run.distance_to_zero,
    }));

    if (path === '/api/categories') return route.fulfill(ok(CATEGORIES));

    if (path === '/api/inbox-summary') return route.fulfill(ok({
      inbox_total: 1598, needs_your_call: 213, categories: [],
    }));

    if (path === '/api/triage/clusters') return route.fulfill(ok([]));
    if (path === '/api/vip') return route.fulfill(ok([]));
    if (path === '/api/profile') return route.fulfill(ok({ text: '', updated_at: null }));
    if (path === '/api/events') {
      return route.fulfill({ status: 200, contentType: 'text/event-stream', body: '' });
    }

    return route.fulfill(ok(null));
  });
}
