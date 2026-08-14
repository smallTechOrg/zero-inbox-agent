'use client'

/**
 * One row of the live classification feed (ui.md screen 14).
 *
 * Renders a single `thread_classified` event: tier badge · subject · category ·
 * action · confidence · reasoning, plus the review-state chip. A `provisional`
 * row is muted and carries an amber NOT YET REVIEWED chip so it can never read
 * as a final decision; `review_failed` carries a red chip. Badges and chips
 * always carry text — never colour alone.
 */

import { useState } from 'react'
import type { ThreadClassifiedEvent } from '@/lib/types'

/** src/domain/enums.py::DecidedBy → the user-facing tier badge. */
const TIER_LABEL: Record<string, string> = {
  rule: 'RULE',
  sender_history: 'SENDER HISTORY',
  llm: 'LLM',
  llm_deep: 'DEEP READ',
  reviewer: 'REVIEWER',
  error: 'ERROR',
}

const TIER_CLASS: Record<string, string> = {
  rule: 'bg-slate-100 text-slate-700 border-slate-300',
  sender_history: 'bg-sky-50 text-sky-700 border-sky-300',
  llm: 'bg-indigo-50 text-indigo-700 border-indigo-300',
  llm_deep: 'bg-violet-50 text-violet-700 border-violet-300',
  reviewer: 'bg-emerald-50 text-emerald-700 border-emerald-300',
  error: 'bg-rose-50 text-rose-700 border-rose-300',
}

export function tierLabel(decidedBy: string): string {
  return TIER_LABEL[decidedBy] ?? decidedBy.toUpperCase().replace(/_/g, ' ')
}

const REASONING_PREVIEW = 80

export function ThreadFeedRow({ event }: { event: ThreadClassifiedEvent }) {
  const [expanded, setExpanded] = useState(false)

  const provisional = event.review_state === 'provisional'
  const failed = event.review_state === 'review_failed'
  const badge = tierLabel(event.decided_by)
  const confidence = Number.isFinite(event.confidence)
    ? `${Math.round(event.confidence * 100)}%`
    : '—'
  const reasoning = event.reasoning ?? ''
  const needsToggle = reasoning.length > REASONING_PREVIEW

  return (
    <li
      data-testid="thread-feed-row"
      data-item-id={event.item_id}
      data-review-state={event.review_state}
      className={`px-4 py-2.5 text-xs ${failed ? 'bg-rose-50' : ''}`}
    >
      <div className="flex flex-wrap items-center gap-1.5">
        <span
          className={`rounded border px-1.5 py-0.5 text-[10px] font-bold tracking-wide ${
            TIER_CLASS[event.decided_by] ?? 'bg-gray-100 text-gray-700 border-gray-300'
          }`}
        >
          {badge}
        </span>
        {provisional && (
          <span className="rounded border border-amber-300 bg-amber-50 px-1.5 py-0.5 text-[10px] font-bold text-amber-800">
            NOT YET REVIEWED
          </span>
        )}
        {failed && (
          <span className="rounded border border-rose-300 bg-rose-100 px-1.5 py-0.5 text-[10px] font-bold text-rose-800">
            REVIEW FAILED — kept
          </span>
        )}
      </div>

      <p
        className={`mt-1 leading-snug ${provisional ? 'text-gray-500' : 'text-gray-900'}`}
        title={event.from_email || undefined}
      >
        <span className="font-medium">{event.subject || '(no subject)'}</span>
        <span className="text-gray-400"> → </span>
        <span>{event.category || 'uncategorised'}</span>
        <span className="text-gray-400"> · </span>
        <span>{event.action || 'keep'}</span>
        <span className="text-gray-400"> · {confidence}</span>
      </p>

      {reasoning ? (
        <p className={`mt-0.5 text-[11px] ${provisional ? 'text-gray-400' : 'text-gray-600'}`}>
          {expanded || !needsToggle ? reasoning : `${reasoning.slice(0, REASONING_PREVIEW)}…`}
          {needsToggle && (
            <button
              type="button"
              onClick={() => setExpanded(v => !v)}
              className="ml-1 font-semibold text-gray-500 underline hover:text-gray-700 focus:ring-1 focus:ring-gray-400 focus:outline-none"
            >
              {expanded ? 'less' : 'more'}
            </button>
          )}
        </p>
      ) : null}
    </li>
  )
}
