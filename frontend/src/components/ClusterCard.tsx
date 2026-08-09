'use client'

import { useCallback, useEffect, useState } from 'react'
import { api } from '@/lib/api'
import type { Cluster, TriageItem } from '@/lib/types'
import { ActionChip } from './TierBadge'
import { EmptyState, ErrorState, SkeletonRows } from './States'
import { ThreadRow } from './ThreadRow'

function confidenceRange(cluster: Cluster) {
  const min = cluster.min_confidence
  const avg = cluster.avg_confidence
  if (min == null && avg == null) return 'confidence unknown'
  const fmt = (v: number | null) => (v == null ? '—' : `${Math.round(v * 100)}%`)
  return `confidence ${fmt(min)} min · ${fmt(avg)} avg`
}

/** How the cluster was grouped — category / sender / domain / mailing list. */
function KindChip({ kind }: { kind: string }) {
  const isCategory = kind === 'category'
  return (
    <span
      data-testid="cluster-kind"
      title={`Grouped by ${kind}`}
      className={`inline-flex shrink-0 items-center rounded border px-1.5 py-0.5 text-[10px] font-bold tracking-wide uppercase ${
        isCategory
          ? 'border-teal-300 bg-teal-50 text-teal-900'
          : 'border-gray-300 bg-gray-50 text-gray-600'
      }`}
    >
      {kind.replace(/_/g, ' ')}
    </span>
  )
}

export function ClusterCard({ cluster }: { cluster: Cluster }) {
  const [open, setOpen] = useState(false)
  const [items, setItems] = useState<TriageItem[] | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const [busy, setBusy] = useState(false)
  const [bulkResult, setBulkResult] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      setItems(await api.clusterItems(cluster.id))
    } catch (e) {
      setError(e)
    } finally {
      setLoading(false)
    }
  }, [cluster.id])

  useEffect(() => {
    if (open && items === null && !loading && !error) void load()
  }, [open, items, loading, error, load])

  const decide = useCallback(
    async (decisionId: string, status: 'approved' | 'rejected') => {
      setBusy(true)
      setError(null)
      try {
        await api.decide(decisionId, status)
        setItems(prev =>
          prev ? prev.map(i => (i.decision_id === decisionId ? { ...i, status } : i)) : prev,
        )
      } catch (e) {
        setError(e)
      } finally {
        setBusy(false)
      }
    },
    [],
  )

  const decideAll = useCallback(
    async (status: 'approved' | 'rejected') => {
      setBusy(true)
      setError(null)
      try {
        const res = await api.decideCluster(cluster.id, status)
        setBulkResult(`${res.updated} thread(s) ${status} — recorded only, Gmail untouched.`)
        setItems(prev => (prev ? prev.map(i => ({ ...i, status })) : prev))
      } catch (e) {
        setError(e)
      } finally {
        setBusy(false)
      }
    },
    [cluster.id],
  )

  return (
    <li
      data-testid="cluster-row"
      className="rounded-lg border border-gray-200 bg-white shadow-sm"
    >
      <div className="flex flex-wrap items-start gap-3 p-3">
        <button
          type="button"
          onClick={() => setOpen(o => !o)}
          aria-expanded={open}
          className="min-w-0 flex-1 text-left focus:ring-2 focus:ring-gray-400 focus:outline-none"
        >
          <span className="flex flex-wrap items-center gap-2">
            <span className="font-mono text-xs text-gray-500">{open ? '−' : '+'}</span>
            <KindChip kind={cluster.kind} />
            <span className="text-sm font-semibold text-gray-900">{cluster.label}</span>
            <span className="rounded bg-gray-100 px-1.5 py-0.5 text-xs font-medium text-gray-700">
              {cluster.item_count} threads
            </span>
            <ActionChip action={cluster.suggested_action} />
            <span className="text-xs text-gray-500">{confidenceRange(cluster)}</span>
          </span>
          {cluster.sample_subjects?.length ? (
            <ul className="mt-1 ml-6 space-y-0.5">
              {cluster.sample_subjects.slice(0, 3).map((s, i) => (
                <li key={i} className="truncate text-xs text-gray-500">
                  {s}
                </li>
              ))}
            </ul>
          ) : null}
        </button>

        <div className="flex shrink-0 gap-1.5">
          <button
            type="button"
            disabled={busy}
            onClick={() => decideAll('approved')}
            className="rounded bg-gray-900 px-2.5 py-1 text-xs font-semibold text-white hover:bg-gray-700 focus:ring-2 focus:ring-gray-400 focus:outline-none disabled:opacity-50"
          >
            {busy ? 'Working…' : 'Approve all'}
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={() => decideAll('rejected')}
            className="rounded border border-gray-300 bg-white px-2.5 py-1 text-xs font-semibold text-gray-800 hover:bg-gray-100 focus:ring-2 focus:ring-gray-400 focus:outline-none disabled:opacity-50"
          >
            Reject all
          </button>
        </div>
      </div>

      {bulkResult ? (
        <p className="px-3 pb-2 text-xs font-medium text-emerald-800">{bulkResult}</p>
      ) : null}

      {open ? (
        <div className="border-t border-gray-200 bg-gray-50/50 p-2">
          {loading ? (
            <SkeletonRows rows={3} label="Loading threads…" />
          ) : error ? (
            <ErrorState error={error} onRetry={() => void load()} />
          ) : items && items.length > 0 ? (
            <ul className="rounded border border-gray-200 bg-white">
              {items.map(item => (
                <ThreadRow
                  key={item.decision_id}
                  item={item}
                  onDecide={decide}
                  busy={busy}
                />
              ))}
            </ul>
          ) : (
            <EmptyState
              title="No threads in this cluster"
              body="Every thread in this cluster has already been swept, or the run has not decided them yet."
            />
          )}
        </div>
      ) : null}
    </li>
  )
}
