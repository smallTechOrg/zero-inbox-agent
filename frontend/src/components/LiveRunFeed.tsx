'use client'

/**
 * The live run feed — ui.md screen 18.
 *
 * *Visible without a click. That is the whole point of this component.*
 *
 * Phase 6 delivered `thread_classified` events, delivered them correctly, and
 * rendered them correctly — into `ActivityDrawer.tsx`, which is `useState(false)`
 * and therefore closed by default. From the user's seat the run was invisible.
 * **Delivered is not shipped; visible is shipped.** This component renders the
 * exact same rows, from the exact same stream, INLINE on the main page, with no
 * click and no toggle, whenever a run is active.
 *
 * It deliberately reuses `useSse()` and `ThreadFeedRow` — there is no second
 * `EventSource`, no second row renderer and no forked feed model. The drawer
 * remains the full-history surface; it is simply no longer the *only* way to
 * see the feed.
 */

import { useEffect, useState } from 'react'
import { isInlineFeedRow, useSse } from '@/lib/SseContext'
import { ThreadFeedRow } from '@/components/ThreadFeedRow'

/** Newest N classification rows shown inline (ui.md screen 18 — "the newest 12"). */
export const LIVE_FEED_ROWS = 12

/**
 * Two missed backend heartbeats (3.0s each) plus margin. At 8s the amber line
 * means the *watchdog itself* stopped, which is genuine signal — a shorter
 * threshold would fire on ordinary jitter and train the user to ignore it.
 */
export const FEED_STALE_SECONDS = 8

function secondsSince(ts: number, now: number): number {
  if (!ts) return 0
  return Math.max(0, Math.floor((now - ts) / 1000))
}

/** "last update {x}s ago", so movement is legible even when rows look alike. */
function lastUpdateLabel(lastEventAt: number, now: number): string {
  if (!lastEventAt) return 'waiting for first update'
  const s = secondsSince(lastEventAt, now)
  if (s < 1) return 'last update just now'
  if (s < 60) return `last update ${s}s ago`
  return `last update ${Math.floor(s / 60)}m ago`
}

/** The heartbeat rendered as plain words — never a bare spinner. */
export function heartbeatLine(hb: {
  phase: string
  detail: string
  batch_size: number | null
  model: string | null
  elapsed_s: number
}): string {
  const parts: string[] = []
  const head = [hb.phase, hb.detail].filter(Boolean).join(' — ')
  if (head) parts.push(head)
  if (hb.batch_size) parts.push(`${hb.batch_size} threads`)
  if (Number.isFinite(hb.elapsed_s)) parts.push(`${Math.round(hb.elapsed_s)}s elapsed`)
  if (hb.model) parts.push(hb.model)
  return parts.join(' · ') || 'working…'
}

export interface LiveRunFeedProps {
  /** True while a run is genuinely executing (not `resumable`, not terminal). */
  active: boolean
  /** Threads enumerated so far, for the header count. */
  threadCount?: number
  /** Opens the Activity drawer for the complete scrollback. */
  onSeeAllActivity?: () => void
  /** Idle one-liner: the previous run's headline, when there is one. */
  idleSummary?: string | null
}

export function LiveRunFeed({
  active,
  threadCount = 0,
  onSeeAllActivity,
  idleSummary,
}: LiveRunFeedProps) {
  const { feed, heartbeat, degraded, lastEventAt, reconnecting } = useSse()

  // Ticks the *clock*, not the server: the stale line and the "last update"
  // stamp are derived from `lastEventAt`, so this polls nothing.
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(id)
  }, [])

  /**
   * An ALLOW-LIST (`INLINE_FEED_KINDS`), not a deny-list. The previous
   * `kind !== 'event'` filter swallowed `thread_archived` and `error` — and
   * would have swallowed every future event kind by default. Generic `log` and
   * progress rows are still excluded: with Phase 7's raised log granularity they
   * would push every classification row off the top 12, which is precisely the
   * visibility this screen exists for. They remain in the drawer's scrollback.
   */
  const rows = feed.filter(isInlineFeedRow).slice(0, LIVE_FEED_ROWS)

  const seeAll = onSeeAllActivity ? (
    <button
      type="button"
      data-testid="see-all-activity"
      onClick={onSeeAllActivity}
      className="text-[11px] font-semibold text-gray-600 underline hover:text-gray-900 focus:ring-2 focus:ring-gray-400 focus:outline-none"
    >
      See all activity →
    </button>
  ) : null

  // ── Degraded provider: a red line pinned to the MAIN PAGE, above the feed.
  // An auto-opened drawer does not satisfy this (ui.md screens 14 + 18).
  const degradedLine = degraded ? (
    <p
      role="status"
      data-testid="page-degraded-banner"
      className="rounded-md border border-rose-300 bg-rose-100 px-3 py-2 text-xs font-semibold text-rose-900"
    >
      {degraded.provider ? degraded.provider.toUpperCase() : 'The LLM provider'} is failing —{' '}
      {degraded.retries} retries. This run is degraded and may take much longer than usual.
      {degraded.model ? ` Model: ${degraded.model}.` : ''}
    </p>
  ) : null

  // ── Idle: one line. Never an empty box, never a progress bar for work that
  // is not running (ui.md screen 18).
  if (!active) {
    return (
      <section
        aria-label="Live run feed"
        data-testid="live-run-feed"
        data-feed-active="false"
        className="space-y-2"
      >
        {degradedLine}
        <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-gray-200 bg-white px-3 py-2">
          <p data-testid="live-feed-idle" className="text-xs text-gray-600">
            {idleSummary ?? 'No run in progress.'}
          </p>
          {seeAll}
        </div>
      </section>
    )
  }

  const silentFor = secondsSince(lastEventAt, now)
  const stale = lastEventAt > 0 && silentFor >= FEED_STALE_SECONDS
  const phase = heartbeat?.phase || 'the current step'

  return (
    <section
      aria-label="Live run feed"
      data-testid="live-run-feed"
      data-feed-active="true"
      className="space-y-2"
    >
      {degradedLine}

      <div className="overflow-hidden rounded-lg border border-gray-200 bg-white">
        {/* Header — thread count · tier · model · live last-update stamp */}
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-gray-200 bg-gray-50 px-3 py-2">
          <h2 data-testid="live-feed-header" className="text-xs font-bold text-gray-800">
            Watching {threadCount.toLocaleString()} thread{threadCount === 1 ? '' : 's'} classify
            {heartbeat?.phase ? ` · ${heartbeat.phase}` : ''}
            {heartbeat?.model ? ` · ${heartbeat.model}` : ''}
          </h2>
          <span className="flex items-center gap-2">
            {reconnecting ? (
              <span className="rounded-full bg-amber-100 px-2 py-0.5 text-[10px] font-semibold text-amber-800">
                Reconnecting…
              </span>
            ) : null}
            <span data-testid="live-feed-last-update" className="text-[11px] text-gray-500">
              {lastUpdateLabel(lastEventAt, now)}
            </span>
            {seeAll}
          </span>
        </div>

        {/* Body — the same rows the drawer renders, from the same stream */}
        {rows.length === 0 ? (
          <p data-testid="live-feed-waiting" className="px-3 py-4 text-xs text-gray-500">
            Reading your inbox — the first classifications appear here within seconds.
          </p>
        ) : (
          <ul data-testid="live-feed-rows" className="divide-y divide-gray-100">
            {rows.map(row => {
              if (row.kind === 'thread') {
                return <ThreadFeedRow key={row.key} event={row.data} />
              }
              if (row.kind === 'thread_archived') {
                return (
                  <li
                    key={row.key}
                    data-testid="live-feed-thread-archived"
                    className="flex items-baseline gap-2 border-l-2 border-emerald-400 bg-emerald-50 px-4 py-2 text-xs text-emerald-900"
                  >
                    <span className="font-semibold">Archived:</span>
                    <span className="truncate">{row.data.subject || '(no subject)'}</span>
                    {row.data.label_name ? (
                      <span className="font-mono text-[11px] text-emerald-700">
                        → {row.data.label_name}
                      </span>
                    ) : null}
                  </li>
                )
              }
              if (row.kind === 'error') {
                return (
                  <li
                    key={row.key}
                    role="alert"
                    data-testid="live-feed-error"
                    className="border-l-2 border-rose-500 bg-rose-50 px-4 py-2 text-xs font-semibold text-rose-900"
                  >
                    Error: {row.data.message}
                  </li>
                )
              }
              if (row.kind === 'model_fallback') {
                return (
                  <li
                    key={row.key}
                    data-testid="live-feed-model-fallback"
                    className="border-l-2 border-amber-400 bg-amber-50 px-4 py-2 text-xs text-amber-900"
                  >
                    <span className="font-semibold">Switched model:</span>{' '}
                    <span className="font-mono">{row.data.from_model || 'unknown'}</span> →{' '}
                    <span className="font-mono">{row.data.to_model || 'unknown'}</span>
                    {row.data.reason ? ` — ${row.data.reason}` : ''}
                  </li>
                )
              }
              if (row.kind !== 'run_resumable') return null
              return (
                <li
                  key={row.key}
                  data-testid="live-feed-run-resumable"
                  className="border-l-2 border-amber-500 bg-amber-50 px-4 py-2 text-xs text-amber-900"
                >
                  <span className="font-semibold">
                    Run interrupted at {row.data.items_decided} of {row.data.items_total} — resumable
                  </span>
                  {row.data.reason ? ` — ${row.data.reason}` : ''}
                </li>
              )
            })}
          </ul>
        )}

        {/* Pinned foot: real state, or an explicit statement of staleness.
            A visually static feed with no explanation is a defect. */}
        {stale ? (
          <p
            role="status"
            data-testid="live-feed-stale"
            className="border-t border-amber-300 bg-amber-50 px-3 py-2 text-[11px] font-semibold text-amber-900"
          >
            No activity for {silentFor}s — still waiting on {phase}.
          </p>
        ) : (
          <p
            role="status"
            data-testid="live-feed-heartbeat"
            className="border-t border-gray-200 bg-gray-50 px-3 py-2 text-[11px] text-gray-600"
          >
            {heartbeat
              ? heartbeatLine(heartbeat)
              : 'Run active — waiting for the first status line from the agent.'}
          </p>
        )}
      </div>
    </section>
  )
}
