'use client'

/**
 * The Activity drawer — the live classification surface (ui.md screen 14).
 *
 * It no longer owns an `EventSource`: the single shared stream lives in
 * `@/lib/SseContext`. The drawer renders the derived feed — one row per decided
 * thread keyed by `item_id` (a reviewer flip replaces the earlier row in place),
 * amber system rows for a mid-run `model_fallback`, a `run_resumable` row with a
 * Resume button, and a pinned degraded-provider banner that auto-opens the
 * drawer so a stretched run is never a silent backend condition.
 */

import { useEffect, useRef, useState } from 'react'
import { api } from '@/lib/api'
import type { ModelFallbackEvent, RunResumableEvent, SseEvent, SseEventType } from '@/lib/types'
import { useSse } from '@/lib/SseContext'
import { ThreadFeedRow } from '@/components/ThreadFeedRow'

function relativeTime(ts: number): string {
  const diff = Math.floor((Date.now() - ts) / 1000)
  if (diff < 5) return 'just now'
  if (diff < 60) return `${diff}s ago`
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`
  return `${Math.floor(diff / 3600)}h ago`
}

const EVENT_ICON: Record<SseEventType | string, string> = {
  run_started: '▶',
  run_progress: '⟳',
  fetch_progress: '↓',
  gmail_mutation_applied: '✓',
  run_completed: '★',
  auto_apply_complete: '⚡',
  thread_classified: '→',
  thread_archived: '✓',
  provider_degraded: '!',
  run_resumable: '⏸',
  model_fallback: '⇄',
  error: '!',
  heartbeat: '♡',
  log: '·',
}

function eventLabel(ev: SseEvent): string | null {
  const p = ev.payload
  switch (ev.type) {
    case 'run_started':
      return `Triage started (triggered by ${p.triggered_by ?? 'unknown'})`
    case 'run_progress':
      return `Progress: ${p.items_decided ?? 0} items decided, $${Number(p.cost_so_far ?? 0).toFixed(4)} spent`
    case 'fetch_progress':
      return `Fetching inbox… ${p.fetched_so_far ?? 0} threads read (page ${p.page ?? 1})`
    case 'run_completed':
      return `Run complete — ${p.total_threads ?? 0} threads, $${Number(p.cost_usd ?? 0).toFixed(4)}`
    case 'auto_apply_complete':
      return `Auto-applied: ${p.applied ?? 0} archived, ${p.auto_kept_low_confidence ?? 0} auto-kept (low confidence)`
    case 'gmail_mutation_applied':
      return `${p.thread_count ?? 0} thread${Number(p.thread_count) !== 1 ? 's' : ''} archived → ${p.category ?? ''}`
    case 'error':
      return `Error: ${p.message ?? 'unknown error'}`
    case 'thread_classified': {
      // Only reached for a malformed event with no item_id — a well-formed one
      // renders as a ThreadFeedRow.
      const subj = String(p.subject ?? '').slice(0, 50)
      return `${subj || '(no subject)'} → ${p.category ?? ''} · ${p.action ?? ''}`
    }
    case 'thread_archived':
      return `Archived: ${String(p.subject ?? '').slice(0, 50) || '(no subject)'} → ${p.label_name ?? ''}`
    case 'heartbeat':
      return null
    case 'log': {
      // Every backend log line, bridged from structlog. Render the event name
      // plus whatever fields the call site logged, so a Gmail 429 backoff or an
      // LLM retry reads as "gmail.retry status=429 attempt=1 backoff_seconds=2"
      // rather than an opaque JSON blob.
      const name = String(p.event ?? 'log')
      const fields = (p.fields ?? {}) as Record<string, unknown>
      const detail = Object.entries(fields)
        .filter(([, v]) => v !== null && v !== undefined && v !== '')
        .map(([k, v]) => `${k}=${typeof v === 'object' ? JSON.stringify(v) : String(v)}`)
        .join(' ')
      return detail ? `${name} · ${detail}` : name
    }
    default:
      return JSON.stringify(ev.payload)
  }
}

function UndoRunButton({ runId }: { runId: string }) {
  const [undoing, setUndoing] = useState(false)
  const [done, setDone] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const handleUndo = async () => {
    const confirmed = window.confirm(
      'This will restore threads to their pre-triage state in Gmail. This cannot be undone. Continue?',
    )
    if (!confirmed) return
    setUndoing(true)
    setError(null)
    try {
      const res = await api.runs.undo(runId)
      setDone(true)
      void res
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Undo failed.')
    } finally {
      setUndoing(false)
    }
  }

  if (done) {
    return <span className="text-[11px] font-semibold text-emerald-700">Restored ✓</span>
  }

  return (
    <div className="mt-1 flex items-center gap-2">
      <button
        type="button"
        onClick={() => void handleUndo()}
        disabled={undoing}
        className="rounded border border-rose-300 bg-rose-50 px-2 py-0.5 text-[11px] font-semibold text-rose-700 hover:bg-rose-100 focus:ring-1 focus:ring-rose-400 focus:outline-none disabled:opacity-50"
      >
        {undoing ? 'Restoring…' : 'Undo run'}
      </button>
      {error && <span className="text-[11px] text-rose-700">{error}</span>}
    </div>
  )
}

/** Resume a run stopped mid-flight. Same call as the Resume banner (screen 13);
 * the endpoint is owned by the durable-resume slice. */
function ResumeRunButton({ runId }: { runId: string }) {
  const [busy, setBusy] = useState(false)
  const [done, setDone] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const handleResume = async () => {
    setBusy(true)
    setError(null)
    try {
      const res = await fetch(`/api/runs/${encodeURIComponent(runId)}/resume`, { method: 'POST' })
      const body = (await res.json().catch(() => null)) as {
        error?: { code?: string; message?: string } | null
      } | null
      if (!res.ok || body?.error) {
        throw new Error(body?.error?.message ?? `Resume failed (${res.status})`)
      }
      setDone(true)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Resume failed.')
    } finally {
      setBusy(false)
    }
  }

  if (done) {
    return <span className="text-[11px] font-semibold text-emerald-700">Resumed ✓</span>
  }

  return (
    <div className="mt-1 flex items-center gap-2">
      <button
        type="button"
        data-testid="drawer-resume-run"
        onClick={() => void handleResume()}
        disabled={busy}
        className="rounded border border-amber-400 bg-amber-100 px-2 py-0.5 text-[11px] font-semibold text-amber-900 hover:bg-amber-200 focus:ring-1 focus:ring-amber-500 focus:outline-none disabled:opacity-50"
      >
        {busy ? 'Resuming…' : 'Resume run'}
      </button>
      {error && <span className="text-[11px] text-rose-700">{error}</span>}
    </div>
  )
}

function ModelFallbackRow({ data, ts }: { data: ModelFallbackEvent; ts: number }) {
  return (
    <li
      data-testid="model-fallback-row"
      className="border-l-2 border-amber-400 bg-amber-50 px-4 py-2.5 text-xs"
    >
      <p className="leading-snug font-semibold text-amber-900">
        Switched model: <span className="font-mono">{data.from_model || 'unknown'}</span> →{' '}
        <span className="font-mono">{data.to_model || 'unknown'}</span>
      </p>
      {data.reason && <p className="mt-0.5 text-[11px] text-amber-800">{data.reason}</p>}
      <p className="mt-0.5 text-[11px] text-amber-700/70">{relativeTime(ts)}</p>
    </li>
  )
}

function RunResumableRow({ data, ts }: { data: RunResumableEvent; ts: number }) {
  return (
    <li
      data-testid="run-resumable-row"
      className="border-l-2 border-amber-500 bg-amber-50 px-4 py-2.5 text-xs"
    >
      <p className="leading-snug font-semibold text-amber-900">
        Run interrupted at {data.items_decided} of {data.items_total} — resumable
      </p>
      {data.reason && <p className="mt-0.5 text-[11px] text-amber-800">{data.reason}</p>}
      {data.run_id ? <ResumeRunButton runId={data.run_id} /> : null}
      <p className="mt-0.5 text-[11px] text-amber-700/70">{relativeTime(ts)}</p>
    </li>
  )
}

export function ActivityDrawer() {
  const { feed, degraded, reconnecting, events } = useSse()
  const [open, setOpen] = useState(false)
  const [seen, setSeen] = useState(0)
  const [now, setNow] = useState(Date.now())

  // Update relative timestamps every 10 s
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 10_000)
    return () => clearInterval(id)
  }, [])
  void now

  const unread = open ? 0 : Math.max(0, events.length - seen)

  // A degraded provider auto-opens the drawer (ui.md screen 14) — once per
  // degraded episode, so the user is never left staring at a stalled bar.
  const lastDegradedRun = useRef<string | null>(null)
  useEffect(() => {
    if (!degraded) {
      lastDegradedRun.current = null
      return
    }
    if (lastDegradedRun.current !== degraded.run_id) {
      lastDegradedRun.current = degraded.run_id
      setOpen(true)
      setSeen(events.length)
    }
  }, [degraded, events.length])

  const handleOpen = () => {
    setOpen(true)
    setSeen(events.length)
  }
  const handleClose = () => {
    setSeen(events.length)
    setOpen(false)
  }

  return (
    <>
      {/* Bell button — fixed in top-right corner */}
      <div className="fixed right-4 top-2 z-30">
        <button
          type="button"
          aria-label={`Activity feed${unread > 0 ? ` (${unread} new)` : ''}`}
          data-testid="activity-bell"
          onClick={handleOpen}
          className="relative flex h-8 w-8 items-center justify-center rounded-full border border-gray-200 bg-white text-gray-600 shadow-sm hover:bg-gray-50 focus:ring-2 focus:ring-gray-400 focus:outline-none"
        >
          <span aria-hidden="true" className="text-base">🔔</span>
          {unread > 0 && (
            <span
              aria-hidden="true"
              className="absolute -right-1 -top-1 flex h-4 min-w-[1rem] items-center justify-center rounded-full bg-rose-500 px-1 text-[10px] font-bold text-white"
            >
              {unread > 9 ? '9+' : unread}
            </span>
          )}
        </button>
      </div>

      {/* Overlay */}
      {open && (
        <div role="presentation" className="fixed inset-0 z-40 bg-black/10" onClick={handleClose} />
      )}

      {/* Drawer */}
      <div
        role="dialog"
        aria-label="Activity feed"
        aria-modal={open}
        className={`fixed right-0 top-0 z-50 flex h-full w-80 flex-col border-l border-gray-200 bg-white shadow-xl transition-transform duration-300 ${
          open ? 'translate-x-0' : 'translate-x-full'
        }`}
      >
        <div className="flex items-center justify-between border-b border-gray-200 px-4 py-3">
          <h2 className="text-sm font-bold text-gray-900">Activity</h2>
          <div className="flex items-center gap-2">
            {reconnecting && (
              <span className="rounded-full bg-amber-100 px-2 py-0.5 text-[11px] font-semibold text-amber-700">
                Reconnecting…
              </span>
            )}
            <button
              type="button"
              aria-label="Close activity drawer"
              onClick={handleClose}
              className="rounded p-1 text-gray-400 hover:bg-gray-100 hover:text-gray-600 focus:ring-2 focus:ring-gray-400 focus:outline-none"
            >
              ✕
            </button>
          </div>
        </div>

        {/* Pinned degraded-provider banner — above the feed, never scrolling */}
        {degraded && (
          <div
            role="status"
            data-testid="provider-degraded-banner"
            className="border-b border-rose-300 bg-rose-100 px-4 py-2.5 text-xs text-rose-900"
          >
            <p className="font-bold">
              {degraded.provider ? degraded.provider.toUpperCase() : 'The LLM provider'} is failing —{' '}
              {degraded.retries} retries.
            </p>
            <p className="mt-0.5 text-[11px]">
              This run is degraded and may take much longer than usual.
              {degraded.model ? ` Model: ${degraded.model}.` : ''}
            </p>
          </div>
        )}

        <div className="flex-1 overflow-y-auto">
          {feed.length === 0 ? (
            <p className="px-4 py-6 text-center text-xs text-gray-400">
              No activity yet — events appear here when a triage run is active.
            </p>
          ) : (
            <ul className="divide-y divide-gray-100">
              {feed.map(row => {
                if (row.kind === 'thread') {
                  return <ThreadFeedRow key={row.key} event={row.data} />
                }
                if (row.kind === 'model_fallback') {
                  return <ModelFallbackRow key={row.key} data={row.data} ts={row.ts} />
                }
                if (row.kind === 'run_resumable') {
                  return <RunResumableRow key={row.key} data={row.data} ts={row.ts} />
                }
                const ev = row.event
                const label = eventLabel(ev)
                const isError = ev.type === 'error'
                const runId = ev.payload.run_id as string | undefined
                return (
                  <li
                    key={row.key}
                    className={`flex items-start gap-2 px-4 py-2.5 text-xs ${
                      isError ? 'bg-rose-50' : ''
                    }`}
                  >
                    <span
                      aria-hidden="true"
                      className={`mt-0.5 shrink-0 font-semibold ${isError ? 'text-rose-600' : 'text-gray-400'}`}
                    >
                      {EVENT_ICON[ev.type] ?? '·'}
                    </span>
                    <div className="min-w-0 flex-1">
                      <p className={`leading-snug ${isError ? 'text-rose-800' : 'text-gray-700'}`}>
                        {label ?? ev.type}
                      </p>
                      <p className="mt-0.5 text-[11px] text-gray-400">{relativeTime(ev.ts)}</p>
                      {ev.type === 'run_completed' && runId ? <UndoRunButton runId={runId} /> : null}
                    </div>
                  </li>
                )
              })}
            </ul>
          )}
        </div>
      </div>
    </>
  )
}
