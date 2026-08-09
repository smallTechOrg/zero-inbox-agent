'use client'

import {
  ApiError,
  type ActionLogRow,
  type ApplyResult,
  type Cluster,
  type Envelope,
  type Me,
  type PriorityProfile,
  type Run,
  type Settings,
  type TriageItem,
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

  startTriage: (connectionId: string, limit = 200, onlyNew = false) =>
    request<{ run_id: string }>(`/api/connections/${connectionId}/triage`, {
      method: 'POST',
      body: JSON.stringify({ limit, only_new: onlyNew }),
    }),

  run: (runId: string) => request<Run>(`/api/runs/${runId}`),

  /** The most recent completed/running run, or null if none exists yet. */
  latestRun: () => request<Run | null>('/api/runs/latest'),

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
  applyDecisions: (decisionIds: string[]) =>
    request<ApplyResult[]>('/api/actions/apply', {
      method: 'POST',
      body: JSON.stringify({ decision_ids: decisionIds }),
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
}

export const AUTH_START_URL = '/auth/google/start'
export { ApiError }
