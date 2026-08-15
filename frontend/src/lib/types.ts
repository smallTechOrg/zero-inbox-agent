/**
 * Types mirroring spec/api.md + spec/data.md exactly. Where the API spec is
 * silent on a JSON field name, the type accepts the data.md column name too
 * and `api.ts` normalizes — see the normalizers there.
 */

export interface Envelope<T> {
  ok: boolean
  data?: T
  error?: { code: string; message: string }
}

export class ApiError extends Error {
  constructor(
    public code: string,
    message: string,
    public status: number,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

export type GmailStatus = 'connected' | 'needs_reconnect' | 'none'

export interface Me {
  email: string
  name: string | null
  picture_url: string | null
  gmail_status: GmailStatus
}

export interface TopSender {
  address: string
  count: number
}

export interface AuditSnapshot {
  total_inbox_threads: number
  unread: number
  oldest_days: number
  top_senders: TopSender[]
  category_tab_counts: Record<string, number>
  created_at: string | null
}

export type CategoryRule = 'label_only' | 'label_and_archive'

export interface Category {
  id: string
  name: string
  description: string
  rule: CategoryRule
  is_needs_review: boolean
  position: number
}

export type RunStatus = 'running' | 'completed' | 'interrupted' | 'undone'

export interface Run {
  id: string
  status: RunStatus
  chunk_limit: number
  started_at: string | null
  finished_at: string | null
  threads_decided: number
  /** Per-category decided counts, e.g. { Finance: 3, Newsletters: 12 } */
  counts: Record<string, number>
  llm_calls: number
  tokens_in: number
  tokens_out: number
  est_cost_usd: number
  fallback_events: number
  interrupt_reason: string | null
  undone_at: string | null
}

/** One persisted feed event (spec/data.md run_events). */
export interface RunEvent {
  seq: number
  type: string // chunk_loaded | decision | action | fallback | cost_tick | run_interrupted | run_finished | undo_*
  sentence: string
  /** reasoning, confidence, category, tokens, cumulative cost, … */
  detail: Record<string, unknown>
  created_at?: string
}
