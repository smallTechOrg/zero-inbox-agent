'use client'

import {
  ApiError,
  type Cluster,
  type Envelope,
  type Me,
  type Run,
  type TriageItem,
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

  startTriage: (connectionId: string, limit = 200) =>
    request<{ run_id: string }>(`/api/connections/${connectionId}/triage`, {
      method: 'POST',
      body: JSON.stringify({ limit }),
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

  decideCluster: (clusterId: string, status: 'approved' | 'rejected') =>
    request<{ updated: number }>(`/api/triage/clusters/${clusterId}/approve`, {
      method: 'POST',
      body: JSON.stringify({ status }),
    }),
}

export const AUTH_START_URL = '/auth/google/start'
export { ApiError }
