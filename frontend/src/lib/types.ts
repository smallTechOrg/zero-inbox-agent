// Shapes mirror spec/api.md (Phase 1). Do not invent fields the backend does not return.

export type Envelope<T> = {
  data: T | null
  error: { code: string; message: string } | null
}

export type Connection = {
  id: string
  account_email: string
  status: string
  connected_at: string | null
}

export type Me = {
  user: { id: string; email: string; display_name: string | null }
  connections: Connection[]
  settings: Record<string, unknown>
}

export type RunStatus = 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled' | string

export type Run = {
  id: string
  status: RunStatus
  dry_run: boolean
  items_total: number | null
  items_decided: number | null
  counts: Record<string, number> | null
  cost: number | null
  error_message: string | null
  started_at: string | null
  finished_at: string | null
}

export type Cluster = {
  id: string
  kind: string
  label: string
  item_count: number
  suggested_action: string
  min_confidence: number | null
  avg_confidence: number | null
  sample_subjects: string[] | null
}

export type DecidedBy =
  | 'rule'
  | 'sender_history'
  | 'llm'
  | 'deep_read'
  | 'reviewer'
  | string

export type TriageItem = {
  decision_id: string
  item: {
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
  status: string
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
