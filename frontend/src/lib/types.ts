// Shapes mirror spec/api.md (Phase 1). Do not invent fields the backend does not return.

export type Envelope<T> = {
  data: T | null
  error: { code: string; message: string } | null
}

export type Connection = {
  id: string
  channel: string
  account_email: string
  status: 'connected' | 'reauth_required' | 'revoked' | string
  connected_at: string | null
}

/** Mirrors the `settings` block of GET /api/me. */
export type Settings = {
  auto_act_threshold: number
  confidence_floor: number
  dry_run: boolean
  llm_model: string
  digest_hour_local: number
  timezone: string
  /** Phase 7 — PATCH /api/settings returns `"above_model_ceiling"` when the
   *  accepted auto_act_threshold is > 0.90. Absent on GET /api/me. */
  warning?: string | null
}

export type Me = {
  user: { id: string; email: string; display_name: string | null }
  connections: Connection[]
  settings: Settings
}

/** src/domain/enums.py::RunStatus — there is no `queued` or `succeeded`. */
export type RunStatus =
  | 'running'
  | 'completed'
  | 'failed'
  | 'cancelled'
  /** Phase 6 — interrupted with durable partial work; resumable in one click. */
  | 'resumable'
  | string

export const RUN_TERMINAL_STATUSES = ['completed', 'failed', 'cancelled'] as const

export function isRunActive(status: RunStatus): boolean {
  return !(RUN_TERMINAL_STATUSES as readonly string[]).includes(status)
}

/** GET /api/runs/{id} returns cost as an object, never a bare number. */
export type RunCost = {
  tokens_in: number
  tokens_out: number
  usd: number
}

export type Run = {
  id: string
  status: RunStatus
  dry_run: boolean
  /** Written early in the run and never null — the backend coalesces to 0. */
  items_total: number
  /** Incremented as each batch of decisions lands. */
  items_decided: number
  counts: Record<string, number>
  cost: RunCost
  error_message: string | null
  started_at: string | null
  finished_at: string | null
  /** Phase 7 — how many threads the agent decided should leave but that are
   *  still sitting in the inbox. Absent on a pre-Phase-7 backend. */
  distance_to_zero?: number
  /** Phase 7 — false when the apply pass recorded a reason OR distance_to_zero > 0. */
  apply_ok?: boolean
}

/** Phase 7 — GET /api/runs/{run_id}/remainder (spec/api.md § Phase 7).
 *
 * Phase 9 adds two buckets (spec/api.md § Run payload additions):
 * `no_never_miss_label` — a never-miss verdict with no resolvable label, so the
 * thread stayed in the inbox rather than being archived unlabelled — and
 * `unreviewed_applied`, the honest historic count of rows applied before the
 * review-gate fix landed. Both are optional so a pre-Phase-9 backend still
 * renders the card. */
export type RemainderBuckets = {
  needs_your_call: number
  category_keep: number
  held_by_never_miss: number
  below_threshold: number
  unclassified: number
  no_never_miss_label?: number
  unreviewed_applied?: number
}

export type RemainderLedger = {
  run_id: string
  inbox_remaining: number
  distance_to_zero: number
  applied: number
  apply_ok: boolean
  apply_failed_reason: string | null
  dry_run: boolean
  remainder: RemainderBuckets
  failures: { decision_id: string; error: string }[]
}

/** The fixed display order of the remainder ledger (ui.md screen 16). */
export const REMAINDER_ORDER: (keyof RemainderBuckets)[] = [
  'needs_your_call',
  'category_keep',
  'below_threshold',
  'held_by_never_miss',
  'no_never_miss_label',
  'unreviewed_applied',
  'unclassified',
]

/** The buckets rendered only when non-zero (ui.md #16 for `unclassified`,
 *  ui.md #28 for the two Phase-9 additions). Every other bucket always renders,
 *  even at zero, labelled "none" — state is never carried by colour alone. */
export const REMAINDER_ONLY_WHEN_NONZERO: (keyof RemainderBuckets)[] = [
  'unclassified',
  'no_never_miss_label',
  'unreviewed_applied',
]

export type ClusterKind = 'list' | 'sender' | 'domain' | 'category' | string

export type Cluster = {
  id: string
  kind: ClusterKind
  label: string
  item_count: number
  suggested_action: string
  min_confidence: number | null
  avg_confidence: number | null
  sample_subjects: string[] | null
}

/** src/domain/enums.py::DecidedBy — the deep tier is `llm_deep`, not `deep_read`. */
export type DecidedBy =
  | 'rule'
  | 'sender_history'
  | 'llm'
  | 'llm_deep'
  | 'reviewer'
  | 'error'
  | string

/** src/domain/enums.py::DecisionStatus */
export type DecisionStatus =
  | 'proposed'
  | 'approved'
  | 'rejected'
  | 'applied'
  | 'undone'
  | 'needs_your_call'
  | string

export type TriageItem = {
  decision_id: string
  cluster_id: string | null
  item: {
    id: string
    subject: string | null
    from_name: string | null
    from_email: string | null
    snippet_redacted: string | null
    internal_date: string | null
    message_count: number | null
    is_unread: boolean | null
  }
  category: string | null
  proposed_action: string
  confidence: number | null
  reasoning: string | null
  decided_by: DecidedBy
  rule_id: string | null
  rule_name: string | null
  time_sensitive: boolean | null
  status: DecisionStatus
}

/** Phase 2: vip_entries (spec/data.md). */
export type VipKind = 'email' | 'domain' | 'keyword'

export type VipEntry = {
  id: string
  kind: VipKind
  value: string
  created_at: string
}

/** Phase 2: priority_profiles (spec/data.md) — one row per user. */
export type PriorityProfile = {
  text: string
  updated_at: string | null
}

/** Phase 2: action_logs (spec/data.md) — the mutation audit trail. */
export type ActionLogRow = {
  id: string
  decision_id: string | null
  operation: string
  request_params: unknown
  response: unknown
  undo_token: unknown
  undone_at: string | null
  created_at: string
}

/** Phase 2+: categories (taxonomy). */
export type DefaultAction = 'archive' | 'keep' | 'digest' | 'needs_your_call'

export type Category = {
  id: string
  key: string
  name: string
  description: string | null
  channel_label_name: string | null
  channel_label_id: string | null
  default_action: DefaultAction
  is_default: boolean
  sort_order: number
  /** Phase 7 — per-category autonomy bar. `null` = inherit the global slider. */
  auto_act_threshold?: number | null
}

/** POST /api/categories/propose → {proposals, model, tokens} */
export type TaxonomyProposal = {
  action: 'add' | 'rename' | 'merge' | 'remove' | 'redescribe'
  key: string
  name: string
  description: string
  reasoning: string
  merge_keys?: string[]
}

/** POST /api/actions/apply → [{action_log_id, undo_token_id}], ordered like the request's decision_ids. */
export type ApplyResult = {
  action_log_id: string
  undo_token_id: string | null
}

// --- Phase 3 ---

export type RunSummaryCategory = { name: string; count: number; suggested_action: string }
export type RunSummaryCluster = { label: string; count: number; suggested_action: string }

export type RunSummary = {
  run_id: string
  status: RunStatus
  total_threads: number
  categories: RunSummaryCategory[]
  top_clusters: RunSummaryCluster[]
  needs_your_call_count: number
  cost_usd: number
  completed_at: string | null
  /** Phase 7 — decisions that actually reached `applied` in Gmail. */
  applied_count?: number
  /** Phase 7 — decided to leave the inbox but still sitting in it. */
  distance_to_zero?: number
  remainder?: RemainderBuckets
}

export type ApproveAndApplyResult = {
  applied: number
  skipped_keep: number
  skipped_needs_your_call: number
  undo_tokens: string[]
}

export type DigestItem = { subject: string; from: string; reason?: string; reasoning?: string }

export type DigestData = {
  run_id: string
  generated_at: string
  time_sensitive_kept: DigestItem[]
  vip_mail: DigestItem[]
  needs_your_call: DigestItem[]
  auto_archived: { count: number; by_category: { name: string; count: number }[] }
}

export type UndoRunResult = {
  reversed: number
  skipped: number
  errors: string[]
}

export type SseEventType =
  | 'run_started'
  | 'run_progress'
  | 'fetch_progress'
  | 'gmail_mutation_applied'
  | 'run_completed'
  | 'auto_apply_complete'
  | 'thread_classified'
  | 'thread_archived'
  /** Phase 6 — the LLM provider is failing enough to stretch the run. */
  | 'provider_degraded'
  /** Phase 6 — the run stopped with durable partial work; it can be resumed. */
  | 'run_resumable'
  /** Phase 6 — a mid-run switch to the next model in the fallback chain. */
  | 'model_fallback'
  /** Phase 7 — apply-pass progress, every 25 applied decisions + once at the end. */
  | 'apply_progress'
  /** Phase 7 — the apply pass could not run, or did not reach zero. */
  | 'run_apply_failed'
  /** Phase 7 — the end-of-run remainder report. */
  | 'inbox_zero_report'
  /** Phase 7 — the watchdog's proof-of-life, carrying real observed state.
   * Never a scrolling row: it is the pinned line at the foot of the feed. */
  | 'activity_heartbeat'
  | 'error'
  | 'heartbeat'
  /** Every backend log line, bridged from structlog — including Gmail 429
   * backoffs and LLM retries, which emit no hand-placed bus event. */
  | 'log'

/** Phase 6 — spec/data.md decisions.review_state. */
export type ReviewState = 'provisional' | 'reviewed' | 'review_failed' | string

/** Payload of a `thread_classified` SSE event.
 * Mirrors spec/capabilities/triage-transparency.md exactly. `subject` is capped
 * at 60 chars and `reasoning` at 140 chars server-side; there is never a body. */
export type ThreadClassifiedEvent = {
  run_id: string
  item_id: string
  subject: string
  from_email: string
  category: string
  action: string
  decided_by: DecidedBy
  confidence: number
  /** Phase 6 — why the tier decided this way (first 140 chars). */
  reasoning: string
  /** Phase 6 — provisional until the never-miss reviewer has seen it. */
  review_state: ReviewState
}

/** Payload of a `thread_archived` SSE event.
 * spec/capabilities/triage-transparency.md § `thread_archived` event shape. */
export type ThreadArchivedEvent = {
  run_id: string
  item_id: string
  subject: string
  category: string
  label_name: string
}

/** An `error` event, surfaced inline — the user needs to tell stuck from slow. */
export type FeedErrorEvent = {
  run_id: string
  message: string
}

export type ProviderDegradedEvent = {
  run_id: string
  provider: string
  model: string
  calls: number
  retries: number
  consecutive_failures: number
}

export type RunResumableEvent = {
  run_id: string
  items_total: number
  items_decided: number
  reason: string
}

/**
 * Phase 7 — the watchdog heartbeat.
 * spec/capabilities/triage-transparency.md § `activity_heartbeat` event shape.
 * Counts, ids, phase names, model ids and elapsed times only — never a subject,
 * sender or body.
 */
export type ActivityHeartbeatEvent = {
  run_id: string
  phase: string
  detail: string
  batch_n: number | null
  batch_total: number | null
  batch_size: number | null
  /** Nullable on the wire (spec/api.md § Phase 7): the watchdog emits `null`
   * outside a batch phase, exactly as it does for the three counts above.
   * Coercing that to `""` would silently invent "no model" as a value. */
  model: string | null
  elapsed_s: number
  silent_for_s: number
}

export type ModelFallbackEvent = {
  run_id: string
  from_model: string
  to_model: string
  reason: string
}

export type SseEvent = {
  id: string // client-side generated
  type: SseEventType
  ts: number // Date.now() when received
  payload: Record<string, unknown>
}

export class ApiError extends Error {
  code: string
  status: number
  constructor(code: string, message: string, status: number) {
    super(message)
    this.name = 'ApiError'
    this.code = code
    this.status = status
  }
}

// ─── Phase 9 — inbox-derived taxonomy and re-organisation ───────────────────
//
// spec/api.md § Phase 9. `lib/api.ts` is not part of this slice's file set, so
// the Phase-9 client lives here beside its types. Same origin (the static export
// is served by the same FastAPI process on :8001), same envelope, same errors.

/** The categories that can never be set to `default_action="archive"`.
 *  Mirrors `src/tools/taxonomy.py::NEVER_ARCHIVE_KEYS` (Phase 9 adds
 *  `important`). A category-wide archive default is a bulk silent sweep of the
 *  mail a human must see; a never-miss archive is per thread, always labelled
 *  and always undoable — different operations, and only the first is blocked. */
export const NEVER_ARCHIVE_KEYS = ['urgent', 'people', 'legal', 'important'] as const

/** The one sentence the UI owes the user next to that disabled control
 *  (spec/ui.md screen 29, spec/roadmap.md Phase 9 step 7). */
export const NEVER_ARCHIVE_NOTE =
  'kept for you — archived under its own label, never swept as a category.'

export function isNeverArchiveKey(key: string): boolean {
  return (NEVER_ARCHIVE_KEYS as readonly string[]).includes(key)
}

/**
 * One sender's worth of evidence behind a proposed category.
 *
 * ASSUMPTION (recorded, not hidden): `spec/api.md` writes `evidence_senders: [...]`
 * without pinning the element shape. We read the rich object form
 * `{email, thread_count}` and also accept a bare address string, so a backend
 * that emits either renders the senders **by name with their thread counts**,
 * which is what the user-test step actually requires.
 */
export type EvidenceSender = {
  email?: string | null
  address?: string | null
  sender?: string | null
  domain?: string | null
  list_id?: string | null
  thread_count?: number | null
  threads?: number | null
  count?: number | null
}

export type EvidenceSenderInput = string | EvidenceSender

/** Normalised for display: an address we can print and a count we can total. */
export function normaliseEvidenceSender(
  raw: EvidenceSenderInput,
): { address: string; threadCount: number | null } {
  if (typeof raw === 'string') return { address: raw, threadCount: null }
  const address =
    raw.email ?? raw.address ?? raw.sender ?? raw.domain ?? raw.list_id ?? 'unknown sender'
  const count = raw.thread_count ?? raw.threads ?? raw.count ?? null
  return { address, threadCount: typeof count === 'number' ? count : null }
}

/** One row of `POST /api/taxonomy/discover`'s proposal. */
export type DiscoveredCategory = {
  key: string
  name: string
  description: string
  default_action: DefaultAction
  rationale: string
  evidence_senders: EvidenceSenderInput[]
  covered_threads: number
  /** Present when the proposal folds existing categories together. */
  merge_keys?: string[] | null
}

export type TaxonomyCoverage = {
  covered_threads: number
  uncovered_threads: number
  gap_threads_resolved: number
  gap_threads_total: number
  /** Optional split of the gap set, when the backend reports it. */
  no_fit_resolved?: number | null
  no_fit_total?: number | null
  low_confidence_resolved?: number | null
  low_confidence_total?: number | null
  total_threads?: number | null
}

export type TaxonomyDiscoveryResult = {
  proposal: DiscoveredCategory[]
  coverage: TaxonomyCoverage
  partial: boolean
  partial_reason: string | null
}

export type TaxonomyApplyDiffRow = {
  action?: string
  key?: string
  name?: string
}

export type TaxonomyApplyResult = {
  created?: number | TaxonomyApplyDiffRow[]
  renamed?: number | TaxonomyApplyDiffRow[]
  retired?: number | TaxonomyApplyDiffRow[]
  diff?: TaxonomyApplyDiffRow[]
  reorg_recommended: boolean
}

/** The closed set of skip reasons (spec/api.md § Phase 9). Nothing skipped is
 *  ever invisible, so the UI renders every one of these, zero included. */
export const REORG_SKIP_REASONS = [
  'not_reviewed',
  'no_category_fit',
  'gmail_error',
  'already_correct',
  'dry_run',
  'cancelled',
] as const

export type ReorgSkipReason = (typeof REORG_SKIP_REASONS)[number] | string

export type ReorgStatus = 'running' | 'completed' | 'partial' | 'cancelled' | 'failed' | string

export type ReorgLedger = {
  job_id?: string
  status: ReorgStatus
  total: number
  done: number
  skipped: Record<ReorgSkipReason, number>
  undoable: boolean
  error_message?: string | null
  /** Optional live detail — rendered when the backend supplies it. */
  phase?: string | null
  current_category?: string | null
  dry_run?: boolean | null
}

export type ReorgUndoResult = {
  reversed: number
  already_undone: number
  failed: { thread_id: string; reason: string }[]
}

export const REORG_TERMINAL: ReorgStatus[] = ['completed', 'partial', 'cancelled', 'failed']

export function isReorgActive(status: ReorgStatus): boolean {
  return !REORG_TERMINAL.includes(status)
}

/** Sum of a skipped map, tolerant of a backend that omits it entirely. */
export function skippedTotal(skipped: Record<string, number> | null | undefined): number {
  if (!skipped) return 0
  return Object.values(skipped).reduce((n, v) => n + (typeof v === 'number' ? v : 0), 0)
}

/** Same envelope contract as `lib/api.ts::request`, kept byte-compatible on
 *  purpose: `{data, error}` with the error code surfaced as `ApiError.code`. */
async function p9Request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response
  try {
    res = await fetch(path, {
      ...init,
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
    })
  } catch {
    throw new ApiError(
      'network_error',
      'Could not reach the server — is it running on http://localhost:8001 ?',
      0,
    )
  }
  let body: Envelope<T> | null = null
  try {
    body = (await res.json()) as Envelope<T>
  } catch {
    body = null
  }
  if (!res.ok || body?.error) {
    throw new ApiError(
      body?.error?.code ?? `http_${res.status}`,
      body?.error?.message ?? `Request to ${path} failed (${res.status}).`,
      res.status,
    )
  }
  return body?.data as T
}

export const phase9Api = {
  taxonomy: {
    /** Proposal only. Mutates nothing — no category, no label, no mail. */
    discover: () =>
      p9Request<TaxonomyDiscoveryResult>('/api/taxonomy/discover', { method: 'POST' }),
    /** Applies the (optionally user-edited) proposal. This is the first call
     *  in the flow that changes anything. */
    apply: (proposal: DiscoveredCategory[]) =>
      p9Request<TaxonomyApplyResult>('/api/taxonomy/apply', {
        method: 'POST',
        body: JSON.stringify({ proposal }),
      }),
  },
  reorg: {
    start: (dryRun?: boolean) =>
      p9Request<{ job_id: string }>('/api/reorg', {
        method: 'POST',
        body: JSON.stringify(dryRun === undefined ? {} : { dry_run: dryRun }),
      }),
    ledger: (jobId: string) =>
      p9Request<ReorgLedger>(`/api/reorg/${encodeURIComponent(jobId)}`),
    cancel: (jobId: string) =>
      p9Request<ReorgLedger>(`/api/reorg/${encodeURIComponent(jobId)}/cancel`, { method: 'POST' }),
    /** Bulk undo — the whole re-organisation as ONE operation, idempotent. */
    undo: (jobId: string) =>
      p9Request<ReorgUndoResult>(`/api/reorg/${encodeURIComponent(jobId)}/undo`, {
        method: 'POST',
      }),
  },
  categories: {
    /** The verification step that must precede any category deletion. */
    usage: (categoryId: string) =>
      p9Request<{ decisions: number; rules: number; items: number }>(
        `/api/categories/${encodeURIComponent(categoryId)}/usage`,
      ),
  },
}
