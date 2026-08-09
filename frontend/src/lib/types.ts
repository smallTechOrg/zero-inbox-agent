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
