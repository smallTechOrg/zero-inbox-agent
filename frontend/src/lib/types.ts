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
}

export type Me = {
  user: { id: string; email: string; display_name: string | null }
  connections: Connection[]
  settings: Settings
}

/** src/domain/enums.py::RunStatus — there is no `queued` or `succeeded`. */
export type RunStatus = 'running' | 'completed' | 'failed' | 'cancelled' | string

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
}

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
  | 'error'
  | 'heartbeat'
  /** Every backend log line, bridged from structlog — including Gmail 429
   * backoffs and LLM retries, which emit no hand-placed bus event. */
  | 'log'

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
