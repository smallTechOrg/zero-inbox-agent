'use client'

/**
 * The single shared SSE connection for the whole app (Phase 6).
 *
 * One module-level `EventSource` is opened lazily on the first subscriber and
 * closed when the last one goes away, so mounting `SseProvider` *and* calling
 * `useSse()` from an unwrapped component never opens two streams to
 * `/api/events`.
 *
 * The context also derives the live-classification feed: thread rows keyed by
 * `item_id` (a later event for the same id — e.g. a reviewer flip — replaces
 * the earlier row **in place**, so the user watches provisional become final),
 * plus the system rows (`model_fallback`, `run_resumable`) and the pinned
 * degraded-provider banner state.
 */

import { createContext, useContext, useEffect, useMemo, useState } from 'react'
import type {
  ActivityHeartbeatEvent,
  FeedErrorEvent,
  ModelFallbackEvent,
  ProviderDegradedEvent,
  RunResumableEvent,
  SseEvent,
  SseEventType,
  ThreadArchivedEvent,
  ThreadClassifiedEvent,
} from '@/lib/types'
import { sseLive } from '@/lib/sseLive'

/** Every type the backend emits as a *named* SSE event. */
export const SSE_EVENT_TYPES: SseEventType[] = [
  'run_started',
  'run_progress',
  'fetch_progress',
  'gmail_mutation_applied',
  'run_completed',
  'auto_apply_complete',
  'thread_classified',
  'thread_archived',
  'provider_degraded',
  'run_resumable',
  'model_fallback',
  'apply_progress',
  'run_apply_failed',
  'inbox_zero_report',
  'activity_heartbeat',
  'error',
  'heartbeat',
  'log',
]

/**
 * Phase 7 raised the backend's log granularity (Rule I2), so a run publishes
 * hundreds of `log` lines alongside the `thread_classified` events. The ring is
 * sized to the backend's replay buffer (1000) so classification rows are not
 * evicted by log noise before the inline feed can render them.
 */
const MAX_EVENTS = 1000

// ── singleton connection ─────────────────────────────────────────────────────

type Snapshot = {
  events: SseEvent[]
  reconnecting: boolean
  /** `Date.now()` of the most recent event of any kind, or 0 if none yet.
   * The stale line and the "last update {x}s ago" stamp are derived from this —
   * the UI never polls the server to find out whether the run is alive. */
  lastEventAt: number
}

type Listener = (s: Snapshot) => void

let nextId = 0
const genId = () => `evt-${++nextId}`

/** Exponential back-off capped at 30 s. */
const backoff = (attempt: number) => Math.min(1000 * Math.pow(2, attempt), 30_000)

let _snapshot: Snapshot = { events: [], reconnecting: false, lastEventAt: 0 }
const _listeners = new Set<Listener>()
let _es: EventSource | null = null
let _timer: ReturnType<typeof setTimeout> | null = null
let _attempt = 0

function publish(next: Partial<Snapshot>) {
  _snapshot = { ..._snapshot, ...next }
  _listeners.forEach(l => l(_snapshot))
}

function addEvent(type: SseEventType, payload: Record<string, unknown>) {
  if (type === 'heartbeat') return
  if (type === 'fetch_progress') {
    sseLive.setFetchedSoFar(Number(payload.fetched_so_far ?? 0))
  }
  if (type === 'run_completed' || type === 'run_started') {
    sseLive.setFetchedSoFar(0)
  }
  const now = Date.now()
  const ev: SseEvent = { id: genId(), type, ts: now, payload }
  // The transport keepalive (`heartbeat`) is deliberately NOT counted above:
  // it ticks whether or not a run is alive, so counting it would mask real
  // silence. `activity_heartbeat` is a real observation and does count.
  publish({ events: [ev, ..._snapshot.events].slice(0, MAX_EVENTS), lastEventAt: now })
}

function connect() {
  if (typeof window === 'undefined') return
  if (_es) _es.close()
  const es = new EventSource('/api/events')
  _es = es

  es.onopen = () => {
    _attempt = 0
    publish({ reconnecting: false })
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

  SSE_EVENT_TYPES.forEach(type => {
    es.addEventListener(type, (e: MessageEvent) => {
      try {
        addEvent(type, JSON.parse(e.data as string) as Record<string, unknown>)
      } catch {
        addEvent(type, {})
      }
    })
  })

  es.onerror = () => {
    es.close()
    if (_es === es) _es = null
    publish({ reconnecting: true })
    _timer = setTimeout(connect, backoff(_attempt++))
  }
}

function subscribe(listener: Listener): () => void {
  _listeners.add(listener)
  if (!_es) connect()
  return () => {
    _listeners.delete(listener)
    if (_listeners.size === 0) {
      if (_es) _es.close()
      _es = null
      if (_timer) clearTimeout(_timer)
      _timer = null
    }
  }
}

// ── derived feed ─────────────────────────────────────────────────────────────

export type FeedRow =
  | { kind: 'thread'; key: string; ts: number; data: ThreadClassifiedEvent }
  /** `event` is carried alongside `data` so the drawer keeps rendering these
   *  through its generic branch exactly as before — the inline feed is the only
   *  surface that changes. */
  | { kind: 'thread_archived'; key: string; ts: number; data: ThreadArchivedEvent; event: SseEvent }
  | { kind: 'error'; key: string; ts: number; data: FeedErrorEvent; event: SseEvent }
  | { kind: 'model_fallback'; key: string; ts: number; data: ModelFallbackEvent }
  | { kind: 'run_resumable'; key: string; ts: number; data: RunResumableEvent }
  /** Any other event (run_progress, log, …) — rendered as a plain row in the drawer. */
  | { kind: 'event'; key: string; ts: number; event: SseEvent }

/**
 * The row kinds the INLINE feed admits (`LiveRunFeed`). This is an ALLOW-LIST on
 * purpose: a deny-list (`kind !== 'event'`) silently swallows every future event
 * type by default, which is exactly how `thread_archived` and `error` ended up
 * drawer-only after Phase 6 shipped them. Adding a kind here is a deliberate act.
 */
export const INLINE_FEED_KINDS: ReadonlyArray<FeedRow['kind']> = [
  'thread',
  'thread_archived',
  'error',
  'model_fallback',
  'run_resumable',
]

export function isInlineFeedRow(row: FeedRow): boolean {
  return INLINE_FEED_KINDS.includes(row.kind)
}

function toThread(p: Record<string, unknown>): ThreadClassifiedEvent {
  return {
    run_id: String(p.run_id ?? ''),
    item_id: String(p.item_id ?? ''),
    subject: String(p.subject ?? ''),
    from_email: String(p.from_email ?? ''),
    category: String(p.category ?? ''),
    action: String(p.action ?? ''),
    decided_by: String(p.decided_by ?? ''),
    confidence: Number(p.confidence ?? 0),
    reasoning: String(p.reasoning ?? ''),
    review_state: String(p.review_state ?? 'provisional'),
  }
}

/**
 * Build the classification feed, newest first. `events` arrives newest-first,
 * so we walk it oldest-first and replace a thread row **in place** when a later
 * event carries the same `item_id`.
 */
export function buildFeed(events: SseEvent[]): FeedRow[] {
  const rows: FeedRow[] = []
  const indexByItem = new Map<string, number>()

  for (let i = events.length - 1; i >= 0; i--) {
    const ev = events[i]
    if (ev.type === 'thread_classified') {
      const data = toThread(ev.payload)
      if (!data.item_id) {
        // Malformed event — still show it rather than swallowing it.
        rows.push({ kind: 'event', key: ev.id, ts: ev.ts, event: ev })
        continue
      }
      const existing = indexByItem.get(data.item_id)
      const row: FeedRow = { kind: 'thread', key: `thread:${data.item_id}`, ts: ev.ts, data }
      if (existing !== undefined) {
        rows[existing] = row // reviewer flip replaces the provisional row in place
      } else {
        indexByItem.set(data.item_id, rows.length)
        rows.push(row)
      }
    } else if (ev.type === 'thread_archived') {
      const p = ev.payload
      rows.push({
        kind: 'thread_archived',
        key: ev.id,
        ts: ev.ts,
        event: ev,
        data: {
          run_id: String(p.run_id ?? ''),
          item_id: String(p.item_id ?? ''),
          subject: String(p.subject ?? ''),
          category: String(p.category ?? ''),
          label_name: String(p.label_name ?? ''),
        },
      })
    } else if (ev.type === 'error') {
      const p = ev.payload
      rows.push({
        kind: 'error',
        key: ev.id,
        ts: ev.ts,
        event: ev,
        data: {
          run_id: String(p.run_id ?? ''),
          message: String(p.message ?? p.error ?? 'unknown error'),
        },
      })
    } else if (ev.type === 'model_fallback') {
      const p = ev.payload
      rows.push({
        kind: 'model_fallback',
        key: ev.id,
        ts: ev.ts,
        data: {
          run_id: String(p.run_id ?? ''),
          from_model: String(p.from_model ?? ''),
          to_model: String(p.to_model ?? ''),
          reason: String(p.reason ?? ''),
        },
      })
    } else if (ev.type === 'run_resumable') {
      const p = ev.payload
      rows.push({
        kind: 'run_resumable',
        key: ev.id,
        ts: ev.ts,
        data: {
          run_id: String(p.run_id ?? ''),
          items_total: Number(p.items_total ?? 0),
          items_decided: Number(p.items_decided ?? 0),
          reason: String(p.reason ?? ''),
        },
      })
    } else if (
      ev.type !== 'heartbeat' &&
      ev.type !== 'provider_degraded' &&
      ev.type !== 'activity_heartbeat'
    ) {
      // provider_degraded is a pinned banner, not a scrolling row (ui.md #14),
      // and activity_heartbeat is the pinned line at the foot of the inline
      // feed (ui.md #18) — neither ever becomes a scrolling row.
      rows.push({ kind: 'event', key: ev.id, ts: ev.ts, event: ev })
    }
  }

  return rows.reverse() // newest first
}

/**
 * The pinned degraded banner: set by the newest `provider_degraded`, cleared by
 * any `run_completed` or `run_resumable` that arrived after it.
 */
export function currentDegraded(events: SseEvent[]): ProviderDegradedEvent | null {
  for (const ev of events) {
    if (ev.type === 'run_completed' || ev.type === 'run_resumable') return null
    if (ev.type === 'provider_degraded') {
      const p = ev.payload
      return {
        run_id: String(p.run_id ?? ''),
        provider: String(p.provider ?? ''),
        model: String(p.model ?? ''),
        calls: Number(p.calls ?? 0),
        retries: Number(p.retries ?? 0),
        consecutive_failures: Number(p.consecutive_failures ?? 0),
      }
    }
  }
  return null
}

/**
 * The newest `activity_heartbeat`, cleared by any `run_completed` /
 * `run_resumable` that arrived after it — a finished run has no live state to
 * report, so the pinned line must not linger with stale numbers.
 */
export function currentHeartbeat(events: SseEvent[]): ActivityHeartbeatEvent | null {
  for (const ev of events) {
    if (ev.type === 'run_completed' || ev.type === 'run_resumable') return null
    if (ev.type === 'activity_heartbeat') {
      const p = ev.payload
      const num = (v: unknown): number | null =>
        v === null || v === undefined || v === '' ? null : Number(v)
      return {
        run_id: String(p.run_id ?? ''),
        phase: String(p.phase ?? ''),
        detail: String(p.detail ?? ''),
        batch_n: num(p.batch_n),
        batch_total: num(p.batch_total),
        batch_size: num(p.batch_size),
        // Nullable on the wire — kept null, never coerced to "".
        model: p.model === null || p.model === undefined ? null : String(p.model),
        elapsed_s: Number(p.elapsed_s ?? 0),
        silent_for_s: Number(p.silent_for_s ?? 0),
      }
    }
  }
  return null
}

// ── React surface ────────────────────────────────────────────────────────────

export type SseContextValue = {
  /** Raw event log, newest first. */
  events: SseEvent[]
  /** Per-thread + system feed rows, newest first, deduped by `item_id`. */
  feed: FeedRow[]
  /** Non-null while the provider is degraded for the current run. */
  degraded: ProviderDegradedEvent | null
  /** Phase 7 — the newest watchdog heartbeat; the pinned line of the inline feed. */
  heartbeat: ActivityHeartbeatEvent | null
  /** Phase 7 — `Date.now()` of the newest event, or 0. Derived, never polled. */
  lastEventAt: number
  reconnecting: boolean
}

const SseCtx = createContext<SseContextValue | null>(null)

function useSseConnection(): SseContextValue {
  const [snap, setSnap] = useState<Snapshot>(_snapshot)

  useEffect(() => subscribe(setSnap), [])

  return useMemo(
    () => ({
      events: snap.events,
      feed: buildFeed(snap.events),
      degraded: currentDegraded(snap.events),
      heartbeat: currentHeartbeat(snap.events),
      lastEventAt: snap.lastEventAt,
      reconnecting: snap.reconnecting,
    }),
    [snap],
  )
}

export function SseProvider({ children }: { children: React.ReactNode }) {
  const value = useSseConnection()
  return <SseCtx.Provider value={value}>{children}</SseCtx.Provider>
}

/**
 * Read the shared SSE stream. Works with or without `SseProvider` above it —
 * the underlying connection is a ref-counted singleton either way.
 */
export function useSse(): SseContextValue {
  const fromProvider = useContext(SseCtx)
  const standalone = useSseConnection()
  return fromProvider ?? standalone
}
