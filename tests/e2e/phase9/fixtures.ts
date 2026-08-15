import type { Page, Route } from '@playwright/test';

/**
 * Deterministic API fixtures for the Phase 9 surfaces.
 *
 * ⚠️  PRODUCTION SAFETY — read before adding a spec to this directory.
 * `zero_inbox.db` holds ~12,500 real decisions for two real accounts, a live
 * Gmail mailbox is connected, and the server on :8001 is the one the user is
 * testing on. Every `/api/**` and `/auth/**` request these specs make is
 * fulfilled **in the browser** and never reaches the server. Nothing here can
 * start a triage run, start a re-organisation, create or delete a category,
 * archive a real message or touch a real Gmail label. That is not a convenience
 * — it is the rule that a Phase-2 spec broke when it left `e2e-actions-test`
 * behind on the user's live account.
 *
 * Shapes are copied from `spec/api.md` § Phase 9 and from the fields the
 * components actually read (`frontend/src/lib/types.ts`).
 */

const ok = (data: unknown) => ({
  status: 200,
  contentType: 'application/json',
  body: JSON.stringify({ data, error: null }),
});

const fail = (status: number, code: string, message: string) => ({
  status,
  contentType: 'application/json',
  body: JSON.stringify({ data: null, error: { code, message } }),
});

export const RUN_ID = 'a9000000-0000-0000-0000-000000000009';
export const JOB_ID = 'job-9000-0000-0000-0000-000000000009';
export const USER_EMAIL = 'phase9@example.com';

/** The live taxonomy the diff is rendered against. */
export const CATEGORIES = [
  { id: 'k1', key: 'people', name: 'People', description: null, channel_label_name: 'ZeroInbox/People', channel_label_id: null, default_action: 'keep', is_default: true, sort_order: 0, auto_act_threshold: null },
  { id: 'k2', key: 'urgent', name: 'Urgent', description: null, channel_label_name: 'ZeroInbox/Urgent', channel_label_id: null, default_action: 'keep', is_default: true, sort_order: 1, auto_act_threshold: null },
  { id: 'k3', key: 'important', name: 'Important', description: null, channel_label_name: 'ZeroInbox/Important', channel_label_id: null, default_action: 'keep', is_default: true, sort_order: 2, auto_act_threshold: null },
  { id: 'k4', key: 'legal', name: 'Legal', description: null, channel_label_name: 'ZeroInbox/Legal', channel_label_id: null, default_action: 'keep', is_default: false, sort_order: 3, auto_act_threshold: null },
  { id: 'k5', key: 'notifications', name: 'Notifications', description: null, channel_label_name: 'ZeroInbox/Notifications', channel_label_id: null, default_action: 'archive', is_default: true, sort_order: 4, auto_act_threshold: null },
];

/**
 * A proposal built from the **measured** sender concentration on the user's own
 * account (spec/roadmap.md § Phase 9 — sender concentration). The point of the
 * user-test step is that he sees *his* senders by name with *their* thread
 * counts, so the fixture uses the real measured addresses and numbers rather
 * than `sender-a@example.com`.
 */
export const PROPOSAL = [
  {
    key: 'social-facebook',
    name: 'Social / Facebook',
    description: 'Everything Facebook mails you.',
    default_action: 'archive',
    rationale: 'Five Facebook addresses account for about 1,586 threads on their own.',
    covered_threads: 1586,
    evidence_senders: [
      { email: 'notification@facebookmail.com', thread_count: 503 },
      { email: 'notification+kr4knbaqrsga@facebookmail.com', thread_count: 489 },
      { email: 'reminders@facebookmail.com', thread_count: 250 },
      { email: 'friendsuggestion@facebookmail.com', thread_count: 184 },
      { email: 'notification@priority.facebookmail.com', thread_count: 130 },
    ],
  },
  {
    key: 'events-tickets',
    name: 'Events & Tickets',
    description: 'Bookings, show reminders and ticket confirmations.',
    default_action: 'archive',
    rationale: 'BookMyShow and Jagriti Theatre are about a fifth of the mailbox.',
    covered_threads: 969,
    evidence_senders: [
      { email: 'no-reply@entertainment.bookmyshow.com', thread_count: 423 },
      { email: 'no-reply@updates.bookmyshow.com', thread_count: 212 },
      { email: 'contact@jagrititheatre.com', thread_count: 334 },
    ],
    merge_keys: ['notifications'],
  },
  {
    key: 'billing-subscriptions',
    name: 'Billing & Subscriptions',
    description: 'Receipts, renewals and payment notices.',
    default_action: 'archive',
    rationale: 'Apple and PayPal bill you regularly and never expect a reply.',
    covered_threads: 254,
    evidence_senders: [
      { email: 'no_reply@email.apple.com', thread_count: 96 },
      { email: 'noreply@email.apple.com', thread_count: 80 },
      { email: 'service@paypal.com', thread_count: 78 },
    ],
  },
  {
    key: 'urgent',
    name: 'Urgent',
    description: 'Security and sign-in notices that must be seen.',
    default_action: 'keep',
    rationale: 'Google and Apple security notices are time-sensitive.',
    covered_threads: 78,
    evidence_senders: [
      { email: 'no-reply@accounts.google.com', thread_count: 39 },
      { email: 'security@facebookmail.com', thread_count: 39 },
    ],
  },
  {
    key: 'people',
    name: 'People',
    description: 'Real humans you correspond with.',
    default_action: 'keep',
    rationale: 'Genuine two-way correspondents.',
    covered_threads: 141,
    evidence_senders: [{ email: 'a.friend@example.com', thread_count: 141 }],
  },
];

export const COVERAGE = {
  covered_threads: 9812,
  uncovered_threads: 524,
  total_threads: 10336,
  gap_threads_resolved: 62,
  gap_threads_total: 62,
  no_fit_resolved: 16,
  no_fit_total: 16,
  low_confidence_resolved: 46,
  low_confidence_total: 46,
};

export type LedgerOverrides = {
  applied?: number;
  distance_to_zero?: number;
  apply_ok?: boolean;
  dry_run?: boolean;
  remainder?: Partial<{
    needs_your_call: number;
    category_keep: number;
    held_by_never_miss: number;
    below_threshold: number;
    unclassified: number;
    no_never_miss_label: number;
    unreviewed_applied: number;
  }>;
};

/** The Phase 9 target state: everything at zero. */
export function ledgerPayload(over: LedgerOverrides = {}) {
  const remainder = {
    needs_your_call: 0,
    category_keep: 0,
    held_by_never_miss: 0,
    below_threshold: 0,
    unclassified: 0,
    no_never_miss_label: 0,
    unreviewed_applied: 0,
    ...(over.remainder ?? {}),
  };
  const distance = over.distance_to_zero ?? 0;
  return {
    run_id: RUN_ID,
    // `inbox_remaining == sum(remainder buckets) + distance_to_zero`, minus the
    // two informational Phase-9 buckets, which are not inbox rows:
    // `unreviewed_applied` counts historic APPLIED rows, and
    // `no_never_miss_label` IS an inbox row, so it counts.
    inbox_remaining:
      remainder.needs_your_call +
      remainder.category_keep +
      remainder.held_by_never_miss +
      remainder.below_threshold +
      remainder.unclassified +
      remainder.no_never_miss_label +
      distance,
    distance_to_zero: distance,
    applied: over.applied ?? 592,
    apply_ok: over.apply_ok ?? true,
    apply_failed_reason: null,
    dry_run: over.dry_run ?? false,
    not_reviewed: 0,
    remainder,
    failures: [],
  };
}

export type ReorgLedgerStub = {
  job_id?: string;
  status: 'running' | 'completed' | 'partial' | 'cancelled' | 'failed';
  total: number;
  done: number;
  skipped: Record<string, number>;
  undoable: boolean;
  error_message?: string | null;
  phase?: string | null;
  current_category?: string | null;
  dry_run?: boolean | null;
};

/** Every request the stub served, so a spec can prove what the UI did and did NOT call. */
export type CallLog = {
  requests: string[];
  /** Bodies of every POST /api/taxonomy/apply — the first call that writes anything. */
  taxonomyApplies: Record<string, unknown>[];
  /** Bodies of every POST /api/reorg. */
  reorgStarts: Record<string, unknown>[];
  /** How many times the bulk-undo endpoint was called. */
  reorgUndos: number;
  reorgCancels: number;
  /** How many times GET /api/reorg/{job} was polled — proof of "no clicks". */
  reorgPolls: number;
};

export type StubOptions = {
  ledger?: LedgerOverrides;
  dryRun?: boolean;
  /** Served in order for successive GET /api/reorg/{id}; the last one repeats. */
  reorgLedgers?: ReorgLedgerStub[];
  /** Fail POST /api/taxonomy/discover with this code. */
  discoverError?: { status: number; code: string; message: string };
  /** `true` ⇒ the proposal is the deterministic fallback, not the full picture. */
  partial?: boolean;
  partialReason?: string | null;
  proposal?: typeof PROPOSAL;
  /** Fail POST /api/reorg with this code (e.g. `reorg_in_progress`). */
  reorgStartError?: { status: number; code: string; message: string };
  undoResult?: { reversed: number; already_undone: number; failed: { thread_id: string; reason: string }[] };
};

export const RUNNING_LEDGER: ReorgLedgerStub = {
  job_id: JOB_ID,
  status: 'running',
  total: 10336,
  done: 1200,
  skipped: { already_correct: 40, not_reviewed: 12 },
  undoable: true,
  phase: 're-classifying',
  current_category: 'Social / Facebook',
  dry_run: false,
  error_message: null,
};

/**
 * Serve a complete signed-in Phase 9 app. Returns the call log.
 *
 * Nothing reaches the server: `/api/**`, `/auth/**` and `/api/events` are all
 * intercepted.
 */
export async function stubApp(page: Page, opts: StubOptions = {}): Promise<CallLog> {
  const log: CallLog = {
    requests: [],
    taxonomyApplies: [],
    reorgStarts: [],
    reorgUndos: 0,
    reorgCancels: 0,
    reorgPolls: 0,
  };

  const settings = {
    auto_act_threshold: 0.8,
    confidence_floor: 0.75,
    dry_run: opts.dryRun ?? false,
    llm_model: 'nvidia/nemotron-3-nano-30b-a3b',
    digest_hour_local: 8,
    timezone: 'Asia/Kolkata',
  };

  const reorgLedgers = opts.reorgLedgers ?? [{ ...RUNNING_LEDGER }];
  let undone = false;

  const run = {
    id: RUN_ID,
    status: 'completed',
    dry_run: settings.dry_run,
    items_total: 894,
    items_decided: 894,
    counts: { applied: 592 },
    cost: { tokens_in: 0, tokens_out: 0, usd: 0 },
    error_message: null,
    started_at: '2026-08-15T08:00:00Z',
    finished_at: '2026-08-15T08:12:00Z',
    distance_to_zero: opts.ledger?.distance_to_zero ?? 0,
    apply_ok: opts.ledger?.apply_ok ?? true,
  };

  await page.route('**/auth/**', async (route: Route) => {
    const url = new URL(route.request().url());
    log.requests.push(`${route.request().method()} ${url.pathname}`);
    return route.fulfill({ status: 200, contentType: 'text/html', body: '<p>oauth stub</p>' });
  });

  await page.route('**/api/**', async (route: Route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    const method = route.request().method();
    log.requests.push(`${method} ${path}`);

    // ── Phase 9 routes (spec/api.md § Phase 9) ────────────────────────────
    if (path === '/api/taxonomy/discover' && method === 'POST') {
      if (opts.discoverError) {
        return route.fulfill(fail(opts.discoverError.status, opts.discoverError.code, opts.discoverError.message));
      }
      return route.fulfill(ok({
        proposal: opts.proposal ?? PROPOSAL,
        coverage: COVERAGE,
        partial: opts.partial ?? false,
        partial_reason: opts.partialReason ?? null,
      }));
    }

    if (path === '/api/taxonomy/apply' && method === 'POST') {
      log.taxonomyApplies.push((route.request().postDataJSON() ?? {}) as Record<string, unknown>);
      return route.fulfill(ok({ created: 3, renamed: 1, retired: 1, reorg_recommended: true }));
    }

    if (path === '/api/reorg' && method === 'POST') {
      log.reorgStarts.push((route.request().postDataJSON() ?? {}) as Record<string, unknown>);
      if (opts.reorgStartError) {
        return route.fulfill(fail(opts.reorgStartError.status, opts.reorgStartError.code, opts.reorgStartError.message));
      }
      return route.fulfill(ok({ job_id: JOB_ID }));
    }

    if (path === `/api/reorg/${JOB_ID}/undo` && method === 'POST') {
      log.reorgUndos += 1;
      const first = !undone;
      undone = true;
      return route.fulfill(ok(
        opts.undoResult ?? {
          reversed: first ? 9812 : 0,
          already_undone: first ? 0 : 9812,
          failed: [],
        },
      ));
    }

    if (path === `/api/reorg/${JOB_ID}/cancel` && method === 'POST') {
      log.reorgCancels += 1;
      const last = reorgLedgers[reorgLedgers.length - 1];
      return route.fulfill(ok({ ...last, status: 'cancelled' }));
    }

    if (path.startsWith('/api/reorg/') && method === 'GET') {
      const i = Math.min(log.reorgPolls, reorgLedgers.length - 1);
      log.reorgPolls += 1;
      return route.fulfill(ok({ job_id: JOB_ID, ...reorgLedgers[i] }));
    }

    // ── Everything the app shell needs to render ──────────────────────────
    if (path === '/api/me') {
      return route.fulfill(ok({
        user: { id: 'u9', email: USER_EMAIL, display_name: 'Phase Nine' },
        connections: [
          {
            id: 'conn-9',
            channel: 'gmail',
            account_email: USER_EMAIL,
            status: 'connected',
            connected_at: '2026-07-01T09:00:00Z',
            last_synced_at: '2026-08-15T08:00:00Z',
          },
        ],
        settings,
      }));
    }
    if (path === '/api/settings' && method === 'PATCH') {
      const body = (route.request().postDataJSON() ?? {}) as Record<string, unknown>;
      Object.assign(settings, body);
      return route.fulfill(ok({ ...settings }));
    }
    if (path === '/api/settings') return route.fulfill(ok({ ...settings }));
    if (path === '/api/runs/latest') return route.fulfill(ok(run));
    if (path === `/api/runs/${RUN_ID}`) return route.fulfill(ok(run));
    if (path === `/api/runs/${RUN_ID}/remainder`) return route.fulfill(ok(ledgerPayload(opts.ledger)));
    if (path === `/api/runs/${RUN_ID}/summary`) {
      return route.fulfill(ok({
        run_id: RUN_ID,
        status: 'completed',
        total_threads: 894,
        categories: [],
        top_clusters: [],
        needs_your_call_count: 0,
        cost_usd: 0,
        completed_at: run.finished_at,
        applied_count: 592,
        distance_to_zero: run.distance_to_zero,
      }));
    }
    if (path === '/api/categories' && method === 'GET') return route.fulfill(ok(CATEGORIES));
    if (path.startsWith('/api/categories') && method !== 'GET') {
      // A Phase 9 spec must never mutate a category, even a stubbed one.
      return route.fulfill(fail(403, 'forbidden', 'category mutation is not permitted in e2e'));
    }
    if (path === '/api/inbox-summary') {
      return route.fulfill(ok({
        inbox_total: 0,
        needs_your_call: 0,
        categories: [
          { key: 'urgent', name: 'Urgent', count: 158 },
          { key: 'important', name: 'Important', count: 46 },
          { key: 'people', name: 'People', count: 23 },
        ],
      }));
    }
    if (path === '/api/triage/clusters') return route.fulfill(ok([]));
    if (path === '/api/vip') return route.fulfill(ok([]));
    if (path === '/api/profile') return route.fulfill(ok({ text: '', updated_at: null }));
    if (path === '/api/account') {
      return route.fulfill(ok({
        user: { id: 'u9', email: USER_EMAIL, display_name: 'Phase Nine', created_at: '2026-06-01T09:00:00Z' },
        connections: [],
        sessions: [],
        counts: { decisions: 894, action_logs: 592, connections: 1 },
      }));
    }
    if (path === '/api/events') {
      return route.fulfill({ status: 200, contentType: 'text/event-stream', body: '' });
    }

    return route.fulfill(ok(null));
  });

  return log;
}

/** Park a running job id where the main page looks for it (no clicks needed). */
export async function seedReorgJob(page: Page, jobId: string = JOB_ID): Promise<void> {
  await page.addInitScript((id: string) => {
    window.localStorage.setItem('zi_reorg_job_id', id);
  }, jobId);
}

/** Clear it again so one spec cannot leak a running job into the next. */
export async function clearReorgJob(page: Page): Promise<void> {
  await page.addInitScript(() => {
    window.localStorage.removeItem('zi_reorg_job_id');
  });
}

export const APP_URL = 'http://localhost:8001/app/';

/**
 * Open the app and wait for the signed-in shell to hydrate.
 *
 * Deliberately NOT `networkidle`: the app holds an `/api/events` SSE stream
 * open, so the network is never idle and that wait would burn its full timeout
 * on every navigation. We wait for a rendered landmark instead.
 */
export async function openApp(page: Page): Promise<void> {
  await page.goto(APP_URL, { waitUntil: 'domcontentloaded' });
  await page.getByRole('navigation', { name: 'Main' }).waitFor({ state: 'visible', timeout: 30_000 });
}

/** Settings → Taxonomy, where discovery lives. */
export async function openSettings(page: Page): Promise<void> {
  await page.getByRole('navigation', { name: 'Main' }).getByRole('button', { name: 'Settings' }).click();
}
