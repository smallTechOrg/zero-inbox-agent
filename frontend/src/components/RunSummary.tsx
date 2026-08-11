'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '@/lib/api'
import { isRunActive, type RunSummary } from '@/lib/types'
import { ErrorState, SkeletonRows } from './States'

interface RunSummaryCardProps {
  runId: string
  onViewHistory?: () => void
}

function fmt(n: number, decimals = 4) {
  return n.toLocaleString('en-US', {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  })
}

function fmtDate(iso: string | null) {
  if (!iso) return '—'
  try {
    return new Date(iso).toLocaleString()
  } catch {
    return iso
  }
}

export function RunSummaryCard({ runId, onViewHistory }: RunSummaryCardProps) {
  const [summary, setSummary] = useState<RunSummary | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<unknown>(null)
  const [undoing, setUndoing] = useState(false)
  const [undoResult, setUndoResult] = useState<string | null>(null)
  const [undoError, setUndoError] = useState<string | null>(null)
  const [undoDone, setUndoDone] = useState(false)
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const load = useCallback(async () => {
    try {
      const s = await api.runSummary(runId)
      setSummary(s)
      setError(null)
      if (!isRunActive(s.status) && pollRef.current) {
        clearInterval(pollRef.current)
        pollRef.current = null
      }
    } catch (e) {
      setError(e)
      if (pollRef.current) {
        clearInterval(pollRef.current)
        pollRef.current = null
      }
    } finally {
      setLoading(false)
    }
  }, [runId])

  useEffect(() => {
    void load()
    pollRef.current = setInterval(async () => {
      try {
        const s = await api.runSummary(runId)
        setSummary(s)
        setError(null)
        if (!isRunActive(s.status) && pollRef.current) {
          clearInterval(pollRef.current)
          pollRef.current = null
        }
      } catch {
        // silently ignore mid-poll errors
      }
    }, 2000)
    return () => {
      if (pollRef.current) clearInterval(pollRef.current)
      pollRef.current = null
    }
  }, [runId, load])

  const handleUndo = useCallback(async () => {
    if (!summary) return
    const confirmed = window.confirm(
      `This will restore ${summary.total_threads} threads to their pre-triage state in Gmail. This cannot be undone. Continue?`,
    )
    if (!confirmed) return
    setUndoing(true)
    setUndoError(null)
    try {
      const res = await api.runs.undo(runId)
      setUndoResult(`${res.reversed} threads restored`)
      setUndoDone(true)
    } catch (e) {
      setUndoError(e instanceof Error ? e.message : 'Undo failed.')
    } finally {
      setUndoing(false)
    }
  }, [runId, summary])

  if (loading) return <SkeletonRows rows={4} label="Loading run summary…" />
  if (error) return <ErrorState error={error} onRetry={() => void load()} />
  if (!summary) return null

  const isActive = isRunActive(summary.status)
  const isCompleted = summary.status === 'completed'

  // Derive counts from categories for the headline
  const archived = summary.categories
    .filter(c => c.suggested_action === 'archive')
    .reduce((n, c) => n + c.count, 0)
  const kept = summary.categories
    .filter(c => c.suggested_action === 'keep')
    .reduce((n, c) => n + c.count, 0)
  const autoKeptLow = summary.needs_your_call_count

  return (
    <section
      aria-label="Run summary"
      className="rounded-xl border border-gray-200 bg-white p-4 shadow-sm"
    >
      {/* Header */}
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <div>
          <h2 className="text-base font-bold text-gray-900">
            {isActive
              ? 'Triage in progress…'
              : summary.status === 'failed'
                ? 'Triage failed'
                : `Triage complete — ${archived} archived, ${kept} kept, ${autoKeptLow} auto-kept`}
          </h2>
          <p className="mt-0.5 text-xs text-gray-500">
            <span
              className={`mr-2 inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-semibold ${
                isActive
                  ? 'bg-blue-100 text-blue-800'
                  : isCompleted
                    ? 'bg-emerald-100 text-emerald-800'
                    : 'bg-rose-100 text-rose-800'
              }`}
            >
              {summary.status}
            </span>
            {summary.completed_at ? `Completed ${fmtDate(summary.completed_at)}` : ''}
            {summary.cost_usd != null ? ` · $${fmt(summary.cost_usd)} spent` : ''}
          </p>
        </div>

        {/* CTAs */}
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => onViewHistory?.()}
            className="rounded-lg border border-gray-300 bg-white px-3 py-1.5 text-sm font-semibold text-gray-700 hover:bg-gray-50 focus:ring-2 focus:ring-gray-400 focus:outline-none"
          >
            View history
          </button>
          {!undoDone && (
            <button
              type="button"
              data-testid="undo-run"
              onClick={() => void handleUndo()}
              disabled={!isCompleted || undoing}
              title={
                !isCompleted
                  ? 'Run must be completed before undoing'
                  : 'Restore all threads to their pre-triage state in Gmail'
              }
              className="rounded-lg border border-rose-300 bg-rose-50 px-3 py-1.5 text-sm font-semibold text-rose-800 hover:bg-rose-100 focus:ring-2 focus:ring-rose-400 focus:outline-none disabled:opacity-50"
            >
              {undoing ? `Restoring ${summary.total_threads} threads…` : 'Undo this run'}
            </button>
          )}
        </div>
      </div>

      {/* In-progress skeleton */}
      {isActive && (
        <div
          aria-label="Triage in progress"
          className="mb-3 h-2 w-full overflow-hidden rounded-full bg-gray-200"
        >
          <div className="h-full animate-pulse rounded-full bg-blue-400" style={{ width: '60%' }} />
        </div>
      )}

      {/* Undo feedback */}
      {undoResult && (
        <p className="mb-3 rounded-md bg-emerald-50 px-3 py-2 text-xs font-medium text-emerald-800">
          {undoResult} ✓
        </p>
      )}
      {undoError && (
        <p className="mb-3 rounded-md bg-rose-50 px-3 py-2 text-xs font-medium text-rose-800">
          {undoError}
        </p>
      )}

      {/* Auto-kept low confidence badge */}
      {summary.needs_your_call_count > 0 && (
        <div className="mb-3 flex items-center gap-2 rounded-md border border-amber-300 bg-amber-50 px-3 py-2">
          <span className="text-amber-700" aria-hidden="true">
            ⚠
          </span>
          <span className="text-xs font-semibold text-amber-800">
            {summary.needs_your_call_count} thread
            {summary.needs_your_call_count !== 1 ? 's' : ''} auto-kept (low confidence) — stayed in
            inbox
          </span>
        </div>
      )}

      <div className="grid gap-4 md:grid-cols-2">
        {/* Category breakdown */}
        {summary.categories.length > 0 && (
          <div>
            <h3 className="mb-1.5 text-xs font-bold tracking-wide text-gray-500 uppercase">
              Categories
            </h3>
            <ul className="space-y-1">
              {summary.categories.map(cat => (
                <li
                  key={cat.name}
                  className="flex items-center justify-between text-xs text-gray-700"
                >
                  <span className="font-medium">{cat.name}</span>
                  <span className="flex items-center gap-2">
                    <span className="font-semibold">{cat.count}</span>
                    <span className="rounded bg-gray-100 px-1.5 py-0.5 text-[11px] text-gray-500">
                      {cat.suggested_action}
                    </span>
                  </span>
                </li>
              ))}
            </ul>
          </div>
        )}

        {/* Top clusters */}
        {summary.top_clusters.length > 0 && (
          <div>
            <h3 className="mb-1.5 text-xs font-bold tracking-wide text-gray-500 uppercase">
              Top clusters
            </h3>
            <ul className="flex flex-wrap gap-1.5">
              {summary.top_clusters.slice(0, 3).map((cl, i) => (
                <li
                  key={i}
                  className="rounded-full border border-gray-200 bg-gray-50 px-2.5 py-1 text-xs"
                  title={`Suggested: ${cl.suggested_action}`}
                >
                  <span className="font-semibold">{cl.count}</span>
                  <span className="mx-1 text-gray-400">·</span>
                  <span className="text-gray-700">{cl.label}</span>
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </section>
  )
}
