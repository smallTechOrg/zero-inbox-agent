'use client'

import {
  ApiError,
  type ActionLogRow,
  type ApplyResult,
  type ApproveAndApplyResult,
  type Category,
  type Cluster,
  type DefaultAction,
  type DigestData,
  type Envelope,
  type Me,
  type PriorityProfile,
  type RemainderLedger,
  type Run,
  type RunSummary,
  type Settings,
  type TaxonomyProposal,
  type TriageItem,
  type UndoRunResult,
  type VipEntry,
  type VipKind,
} from './types'

/**
 * The static export is served by the same FastAPI process that serves the API
 * (http://localhost:8001/app/), so every path below is same-origin and absolute.
 */
async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response
  try {
    res = await fetch(path, {
      ...init,
      credentials: 'same-origin',
      headers: {
        'Content-Type': 'application/json',
        ...(init?.headers ?? {}),
      },
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
    const code = body?.error?.code ?? `http_${res.status}`
    const message = body?.error?.message ?? `Request to ${path} failed (${res.status}).`
    throw new ApiError(code, message, res.status)
  }
  return body?.data as T
}

export const api = {
  me: () => request<Me>('/api/me'),

  startTriage: (connectionId: string, limit = 10_000, onlyNew = false) =>
    request<{ run_id: string }>(`/api/connections/${connectionId}/triage`, {
      method: 'POST',
      body: JSON.stringify({ limit, only_new: onlyNew }),
    }),

  run: (runId: string) => request<Run>(`/api/runs/${runId}`),

  /** The most recent completed/running run, or null if none exists yet. */
  latestRun: () => request<Run | null>('/api/runs/latest'),

  /** Live counts straight from Gmail: total left in inbox, plus a per-category
   * label breakdown. Each count is one cheap labels().get() call server-side. */
  inboxSummary: () =>
    request<{
      inbox_total: number
      needs_your_call: number
      categories: { key: string; name: string; count: number; channel_label_name: string }[]
    }>('/api/inbox-summary'),

  cancelRun: (runId: string) =>
    request<{ status: string }>(`/api/runs/${runId}/cancel`, { method: 'POST' }),

  clusters: (runId: string) =>
    request<Cluster[]>(`/api/triage/clusters?run_id=${encodeURIComponent(runId)}`),

  clusterItems: (clusterId: string) =>
    request<TriageItem[]>(`/api/triage/items?cluster_id=${encodeURIComponent(clusterId)}`),

  itemsByStatus: (runId: string, status: string) =>
    request<TriageItem[]>(
      `/api/triage/items?run_id=${encodeURIComponent(runId)}&status=${encodeURIComponent(status)}`,
    ),

  decide: (decisionId: string, status: 'approved' | 'rejected') =>
    request<TriageItem>(`/api/triage/decisions/${decisionId}`, {
      method: 'POST',
      body: JSON.stringify({ status }),
    }),

  /** Approve/reject every cluster in a run in one call — needs_your_call is
   * always excluded server-side, same rule as the per-cluster endpoint. */
  reviewAllClusters: (runId: string, status: 'approved' | 'rejected') =>
    request<{ updated: number; skipped_needs_your_call: number }>(
      `/api/triage/runs/${runId}/review-all`,
      { method: 'POST', body: JSON.stringify({ status }) },
    ),

  decideCluster: (clusterId: string, status: 'approved' | 'rejected') =>
    request<{ updated: number }>(`/api/triage/clusters/${clusterId}/approve`, {
      method: 'POST',
      body: JSON.stringify({ status }),
    }),

  // --- Phase 2 ---

  updateSettings: (patch: Partial<Settings>) =>
    request<Settings>('/api/settings', {
      method: 'PATCH',
      body: JSON.stringify(patch),
    }),

  /**
   * Performs the real Gmail mutations for already-approved decisions. Only
   * ever called when dry_run is off — the server itself enforces this
   * (409 dry_run_violation) but the client never calls it in dry-run mode
   * either, and never for a `rejected` decision.
   */
  applyDecisions: (decisionIds: string[], force = false) =>
    request<ApplyResult[]>('/api/actions/apply', {
      method: 'POST',
      body: JSON.stringify({ decision_ids: decisionIds, force }),
    }),

  undoAction: (actionLogId: string) =>
    request<ActionLogRow>(`/api/actions/${actionLogId}/undo`, { method: 'POST' }),

  actions: () => request<ActionLogRow[]>('/api/actions'),

  vip: {
    list: () => request<VipEntry[]>('/api/vip'),
    add: (kind: VipKind, value: string) =>
      request<VipEntry>('/api/vip', {
        method: 'POST',
        body: JSON.stringify({ kind, value }),
      }),
    remove: (id: string) => request<{ deleted: boolean }>(`/api/vip/${id}`, { method: 'DELETE' }),
  },

  profile: {
    get: () => request<PriorityProfile>('/api/profile'),
    put: (text: string) =>
      request<PriorityProfile>('/api/profile', {
        method: 'PUT',
        body: JSON.stringify({ text }),
      }),
  },

  // --- Phase 3 ---

  runSummary: (runId: string) => request<RunSummary>(`/api/runs/${runId}/summary`),

  approveAndApply: (runId: string) =>
    request<ApproveAndApplyResult>(`/api/runs/${runId}/approve-and-apply`, { method: 'POST' }),

  runs: {
    undo: (runId: string) =>
      request<UndoRunResult>(`/api/runs/${runId}/undo`, { method: 'POST' }),

    // --- Phase 7 (spec/api.md § Phase 7 — Drive to Inbox Zero) ---

    /** The single honest answer to "how far from zero am I, and why?".
     *  Computed live from `decisions`, never from cached counts. */
    remainder: (runId: string) =>
      request<RemainderLedger>(`/api/runs/${encodeURIComponent(runId)}/remainder`),

    /** Re-runs the apply pass for a `completed` run without re-classifying a
     *  single thread. Idempotent — the recovery path behind "Retry archiving". */
    apply: (runId: string) =>
      request<Record<string, unknown>>(`/api/runs/${encodeURIComponent(runId)}/apply`, {
        method: 'POST',
      }),
  },

  digestLatest: () => request<DigestData>('/api/digest/latest'),

  categories: {
    list: () => request<Category[]>('/api/categories'),
    create: (name: string, defaultAction: DefaultAction = 'keep', key?: string) =>
      request<Category>('/api/categories', {
        method: 'POST',
        body: JSON.stringify({ name, default_action: defaultAction, key: key ?? name.toLowerCase().replace(/\s+/g, '_').replace(/[^a-z0-9_]/g, '') }),
      }),
    patch: (
      id: string,
      patch: {
        name?: string
        default_action?: DefaultAction
        sort_order?: number
        /** Phase 7 — per-category autonomy bar; `null` clears it back to inherit. */
        auto_act_threshold?: number | null
      },
    ) =>
      request<Category>(`/api/categories/${id}`, {
        method: 'PATCH',
        body: JSON.stringify(patch),
      }),
    propose: () =>
      request<{ proposals: TaxonomyProposal[]; model: string; tokens: number }>('/api/categories/propose', {
        method: 'POST',
      }),
  },
}

export const AUTH_START_URL = '/auth/google/start'
export { ApiError }
