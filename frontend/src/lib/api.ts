'use client'

import {
  ApiError,
  type AuditSnapshot,
  type Category,
  type CategoryRule,
  type Envelope,
  type Me,
  type Run,
} from './types'

/** Sign-in and OAuth entry points — spec/api.md § Auth & Account. */
export const LOGIN_URL = '/auth/google/login'

async function request<T>(path: string, init?: RequestInit): Promise<T> {
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
      'Could not reach the server — is the backend running on port 8001?',
      0,
    )
  }

  let body: Envelope<T> | null = null
  try {
    body = (await res.json()) as Envelope<T>
  } catch {
    body = null
  }

  if (!res.ok || body?.ok === false || body?.error) {
    const code = body?.error?.code ?? `http_${res.status}`
    const message = body?.error?.message ?? `Request failed (${res.status}).`
    throw new ApiError(code, message, res.status)
  }
  return body?.data as T
}

export function isSignedOut(e: unknown): boolean {
  return e instanceof ApiError && (e.code === 'signed_out' || e.status === 401)
}

export function isGmailReconnect(e: unknown): boolean {
  return e instanceof ApiError && e.code === 'gmail_reconnect'
}

/* ── Normalizers — tolerate the data.md column names (`*_json`) if the API
   returns rows verbatim; the shape the UI consumes is fixed here. ────────── */

function asRecord(v: unknown): Record<string, unknown> {
  if (typeof v === 'string') {
    try {
      return JSON.parse(v) as Record<string, unknown>
    } catch {
      return {}
    }
  }
  return (v as Record<string, unknown>) ?? {}
}

function normalizeMe(raw: unknown): Me {
  const r = asRecord(raw)
  const user = asRecord(r.user ?? r)
  const status = (r.gmail_status ?? r.gmail ?? asRecord(r.gmail_account).status ?? 'none') as
    | Me['gmail_status']
    | undefined
  return {
    email: String(user.email ?? ''),
    name: (user.name as string) ?? null,
    picture_url: (user.picture_url as string) ?? null,
    gmail_status: status === 'connected' || status === 'needs_reconnect' ? status : 'none',
  }
}

function normalizeAudit(raw: unknown): AuditSnapshot | null {
  if (raw == null) return null
  const r = asRecord(raw)
  let senders = (r.top_senders ?? r.top_senders_json) as unknown
  if (typeof senders === 'string') {
    try {
      senders = JSON.parse(senders)
    } catch {
      senders = []
    }
  }
  const senderList: unknown[] = Array.isArray(senders) ? senders : []
  return {
    total_inbox_threads: Number(r.total_inbox_threads ?? 0),
    unread: Number(r.unread ?? 0),
    oldest_days: Number(r.oldest_days ?? 0),
    top_senders: senderList.map((s) => {
      const row = asRecord(s)
      return { address: String(row.address ?? row.sender ?? ''), count: Number(row.count ?? 0) }
    }),
    category_tab_counts: asRecord(r.category_tab_counts ?? r.category_tab_counts_json) as Record<
      string,
      number
    >,
    created_at: (r.created_at as string) ?? null,
  }
}

function normalizeRun(raw: unknown): Run {
  const r = asRecord(raw)
  return {
    id: String(r.id ?? r.run_id ?? ''),
    status: (r.status as Run['status']) ?? 'completed',
    chunk_limit: Number(r.chunk_limit ?? 50),
    started_at: (r.started_at as string) ?? null,
    finished_at: (r.finished_at as string) ?? null,
    threads_decided: Number(r.threads_decided ?? 0),
    counts: asRecord(r.counts ?? r.counts_json) as Record<string, number>,
    llm_calls: Number(r.llm_calls ?? 0),
    tokens_in: Number(r.tokens_in ?? 0),
    tokens_out: Number(r.tokens_out ?? 0),
    est_cost_usd: Number(r.est_cost_usd ?? 0),
    fallback_events: Number(r.fallback_events ?? 0),
    interrupt_reason: (r.interrupt_reason as string) ?? null,
    undone_at: (r.undone_at as string) ?? null,
  }
}

/* ── The API surface, exactly spec/api.md Phase 1 ────────────────────────── */

export const api = {
  me: async () => normalizeMe(await request<unknown>('/api/me')),
  logout: () => request<unknown>('/api/auth/logout', { method: 'POST' }),
  disconnectGmail: () => request<unknown>('/api/gmail/disconnect', { method: 'POST' }),

  runAudit: async () => normalizeAudit(await request<unknown>('/api/audit', { method: 'POST' })),
  latestAudit: async () => normalizeAudit(await request<unknown>('/api/audit/latest')),

  taxonomy: {
    list: () => request<Category[]>('/api/taxonomy'),
    add: (body: { name: string; description: string; rule: CategoryRule }) =>
      request<Category>('/api/taxonomy', { method: 'POST', body: JSON.stringify(body) }),
    patch: (id: string, patch: Partial<Pick<Category, 'name' | 'description' | 'rule'>>) =>
      request<Category>(`/api/taxonomy/${id}`, { method: 'PATCH', body: JSON.stringify(patch) }),
    remove: (id: string) => request<unknown>(`/api/taxonomy/${id}`, { method: 'DELETE' }),
  },

  runs: {
    /** 409-with-active-run_id is surfaced by the caller via ApiError. */
    start: (chunkLimit?: number) =>
      request<{ run_id: string }>('/api/runs', {
        method: 'POST',
        body: JSON.stringify(chunkLimit ? { chunk_limit: chunkLimit } : {}),
      }),
    list: async () => ((await request<unknown[]>('/api/runs')) ?? []).map(normalizeRun),
    get: async (id: string) => normalizeRun(await request<unknown>(`/api/runs/${id}`)),
    undo: (id: string) => request<unknown>(`/api/runs/${id}/undo`, { method: 'POST' }),
    /** SSE endpoint URL — consumed by useRunFeed, not fetch. */
    eventsUrl: (id: string, afterSeq: number) =>
      `/api/runs/${encodeURIComponent(id)}/events?after_seq=${afterSeq}`,
  },
}

export { ApiError }
