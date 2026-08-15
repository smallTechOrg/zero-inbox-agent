'use client'

import { useMemo } from 'react'
import type { RunEvent } from '../lib/types'
import type { RunFeed } from '../lib/useRunFeed'
import { ErrorNote, Loading, Section } from './ui'

function num(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null
}

/** Reasoning + confidence live in detail_json behind an expander (spec). */
function FeedRow({ ev }: { ev: RunEvent }) {
  const reasoning = (ev.detail.reasoning ?? ev.detail.reason) as string | undefined
  const confidence = num(ev.detail.confidence)
  const expandable = Boolean(reasoning) || confidence != null
  const isFallback = ev.type === 'fallback'
  const isTerminalBad = ev.type === 'run_interrupted' || ev.type === 'undo_failed'

  const sentence = (
    <span
      className={
        isFallback
          ? 'text-zi-warn'
          : isTerminalBad
            ? 'text-zi-danger'
            : ev.type.startsWith('undo') || ev.type === 'run_finished'
              ? 'text-zi-ok'
              : undefined
      }
    >
      {ev.sentence}
    </span>
  )

  if (!expandable) {
    return <li className="zi-body zi-row-enter py-1.5">{sentence}</li>
  }
  return (
    <li className="zi-row-enter py-1.5">
      <details>
        <summary className="zi-body zi-focusable cursor-pointer list-item rounded-zi-r-sm">
          {sentence}
        </summary>
        <div className="zi-caption mt-1.5 ml-4 rounded-zi-r-sm bg-zi-bg-subtle px-3 py-2 text-zi-fg-muted">
          {reasoning && <p>{reasoning}</p>}
          {confidence != null && (
            <p className="zi-mono mt-1">confidence {confidence.toFixed(2)}</p>
          )}
        </div>
      </details>
    </li>
  )
}

/**
 * Live activity feed (spec/ui.md §3): streaming sentences, progress n/limit,
 * per-category running counts, and the cost ticker (calls / tokens / est. $)
 * that also surfaces NVIDIA→Gemini fallback events.
 */
export function ActivityFeed({
  feed,
  chunkLimit,
  title = 'Live activity',
}: {
  feed: RunFeed
  chunkLimit: number
  title?: string
}) {
  const { events, live, connectionError } = feed

  const derived = useMemo(() => {
    let decided = 0
    let total = chunkLimit
    const categories: Record<string, number> = {}
    // Cost ticker: cost_tick events carry CUMULATIVE totals (last one wins).
    let calls = 0
    let tokens = 0
    let cost = 0
    let fallbacks = 0
    for (const ev of events) {
      if (ev.type === 'chunk_loaded') {
        const t = num(ev.detail.total) ?? num(ev.detail.count) ?? num(ev.detail.threads)
        if (t != null) total = t
      } else if (ev.type === 'decision') {
        decided += 1
        const cat = (ev.detail.category ?? ev.detail.category_name) as string | undefined
        if (cat) categories[cat] = (categories[cat] ?? 0) + 1
      } else if (ev.type === 'cost_tick') {
        calls = num(ev.detail.llm_calls) ?? num(ev.detail.calls) ?? calls + 1
        const tin = num(ev.detail.tokens_in) ?? 0
        const tout = num(ev.detail.tokens_out) ?? 0
        tokens = num(ev.detail.tokens) ?? tin + tout
        cost = num(ev.detail.est_cost_usd) ?? cost
      } else if (ev.type === 'fallback') {
        fallbacks += 1
      }
    }
    return { decided, total, categories, calls, tokens, cost, fallbacks }
  }, [events, chunkLimit])

  const pct = derived.total > 0 ? Math.min(100, (derived.decided / derived.total) * 100) : 0

  return (
    <Section
      title={title}
      aside={
        live ? (
          <span className="zi-caption flex items-center gap-1.5 text-zi-ok">
            <span className="zi-spinner" aria-hidden /> live
          </span>
        ) : undefined
      }
    >
      {/* Progress — reflects real decisions, never faked. */}
      <div className="mb-3">
        <div className="mb-1 flex items-center justify-between">
          <p className="zi-caption text-zi-fg-muted">
            {derived.decided} of {derived.total} threads decided
          </p>
          <p className="zi-mono text-zi-fg-muted">
            {derived.calls} calls · {derived.tokens.toLocaleString()} tokens · $
            {derived.cost.toFixed(4)}
            {derived.fallbacks > 0 && (
              <span className="text-zi-warn"> · {derived.fallbacks} fallback</span>
            )}
          </p>
        </div>
        <div
          role="progressbar"
          aria-valuemin={0}
          aria-valuemax={derived.total}
          aria-valuenow={derived.decided}
          aria-label="Run progress"
          className="h-1.5 overflow-hidden rounded-full bg-zi-bg-subtle"
        >
          <div
            className="h-full rounded-full bg-zi-accent transition-[width]"
            style={{ width: `${pct}%` }}
          />
        </div>
      </div>

      {Object.keys(derived.categories).length > 0 && (
        <ul className="mb-3 flex flex-wrap gap-2">
          {Object.entries(derived.categories).map(([name, count]) => (
            <li
              key={name}
              className="zi-caption rounded-zi-r-sm border border-zi-border bg-zi-bg-subtle px-2 py-0.5 text-zi-fg-muted"
            >
              {name} <span className="zi-num font-semibold text-zi-fg">{count}</span>
            </li>
          ))}
        </ul>
      )}

      {connectionError && <ErrorNote message={connectionError} />}

      {events.length === 0 ? (
        <Loading label="Waiting for the first action…" />
      ) : (
        <ul
          data-testid="activity-feed"
          className="max-h-96 divide-y divide-zi-border overflow-y-auto"
        >
          {[...events].reverse().map((ev) => (
            <FeedRow key={ev.seq} ev={ev} />
          ))}
        </ul>
      )}
    </Section>
  )
}
