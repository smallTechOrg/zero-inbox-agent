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

/** Phase 7 — GET /api/runs/{run_id}/remainder (spec/api.md § Phase 7). */
export type RemainderBuckets = {
  needs_your_call: number
  category_keep: number
  held_by_never_miss: number
  below_threshold: number
  unclassified: number
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
  'unclassified',
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
