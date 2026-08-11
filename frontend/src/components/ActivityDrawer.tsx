'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '@/lib/api'
import type { SseEvent, SseEventType } from '@/lib/types'
import { sseLive } from '@/lib/sseLive'

const MAX_EVENTS = 50

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
  error: '!',
  heartbeat: '♡',
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
      const action = String(p.action ?? '')
      const cat = String(p.category ?? '')
      const subj = String(p.subject ?? '').slice(0, 50)
      const verb = action === 'archive' ? 'archive' : action === 'keep' ? 'keep' : action
      return `${subj || '(no subject)'} → ${cat} · ${verb}`
    }
    case 'thread_archived':
      return `Archived: ${String(p.subject ?? '').slice(0, 50) || '(no subject)'} → ${p.label_name ?? ''}`
    case 'heartbeat':
      return null
    default:
      return JSON.stringify(ev.payload)
  }
}

let nextId = 0
function genId() {
  return `evt-${++nextId}`
}

/** Exponential back-off capped at 30 s */
function backoff(attempt: number) {
  return Math.min(1000 * Math.pow(2, attempt), 30_000)
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

export function ActivityDrawer() {
  const [events, setEvents] = useState<SseEvent[]>([])
  const [open, setOpen] = useState(false)
  const [unread, setUnread] = useState(0)
  const [reconnecting, setReconnecting] = useState(false)
  const [now, setNow] = useState(Date.now())

  // Update relative timestamps every 10 s
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 10_000)
    return () => clearInterval(id)
  }, [])
  // Suppress unused warning — now used as dep below
  void now

  const attemptRef = useRef(0)
  const esRef = useRef<EventSource | null>(null)
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  const addEvent = useCallback(
    (type: SseEventType, payload: Record<string, unknown>) => {
      if (type === 'heartbeat') return
      if (type === 'fetch_progress') {
        sseLive.setFetchedSoFar(Number(payload.fetched_so_far ?? 0))
      }
      if (type === 'run_completed' || type === 'run_started') {
        sseLive.setFetchedSoFar(0)
      }
      const ev: SseEvent = { id: genId(), type, ts: Date.now(), payload }
      setEvents(prev => [ev, ...prev].slice(0, MAX_EVENTS))
      setUnread(n => n + 1)
    },
    [],
  )

  const connect = useCallback(() => {
    if (esRef.current) esRef.current.close()
    const es = new EventSource('/api/events')
    esRef.current = es

    es.onopen = () => {
      attemptRef.current = 0
      setReconnecting(false)
    }

    es.onmessage = e => {
      try {
        const parsed = JSON.parse(e.data as string) as { type: SseEventType; [k: string]: unknown }
        const { type, ...rest } = parsed
        addEvent(type, rest)
      } catch {
        // ignore malformed event
      }
    }

    // Named event handlers for specific event types
    const EVENT_TYPES: SseEventType[] = [
      'run_started',
      'run_progress',
      'fetch_progress',
      'gmail_mutation_applied',
      'run_completed',
      'auto_apply_complete',
      'thread_classified',
      'thread_archived',
      'error',
      'heartbeat',
    ]
    EVENT_TYPES.forEach(type => {
      es.addEventListener(type, (e: MessageEvent) => {
        try {
          const payload = JSON.parse((e as MessageEvent).data as string) as Record<string, unknown>
          addEvent(type, payload)
        } catch {
          addEvent(type, {})
        }
      })
    })

    es.onerror = () => {
      es.close()
      esRef.current = null
      setReconnecting(true)
      const delay = backoff(attemptRef.current++)
      timerRef.current = setTimeout(() => {
        connect()
      }, delay)
    }
  }, [addEvent])

  useEffect(() => {
    connect()
    return () => {
      if (esRef.current) esRef.current.close()
      if (timerRef.current) clearTimeout(timerRef.current)
    }
  }, [connect])

  const handleOpen = () => {
    setOpen(true)
    setUnread(0)
  }
  const handleClose = () => setOpen(false)

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
        <div
          role="presentation"
          className="fixed inset-0 z-40 bg-black/10"
          onClick={handleClose}
        />
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

        <div className="flex-1 overflow-y-auto">
          {events.length === 0 ? (
            <p className="px-4 py-6 text-center text-xs text-gray-400">
              No activity yet — events appear here when a triage run is active.
            </p>
          ) : (
            <ul className="divide-y divide-gray-100">
              {events.map(ev => {
                const label = eventLabel(ev)
                const isError = ev.type === 'error'
                const runId = ev.payload.run_id as string | undefined
                return (
                  <li
                    key={ev.id}
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
                      {ev.type === 'run_completed' && runId ? (
                        <UndoRunButton runId={runId} />
                      ) : null}
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
