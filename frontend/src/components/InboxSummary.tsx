'use client'

import { useEffect, useState } from 'react'
import { api } from '@/lib/api'
import { ApiError } from '@/lib/types'

export interface InboxSummaryData {
  inbox_total: number
  needs_your_call: number
  categories: { key: string; name: string; count: number; channel_label_name: string }[]
}

const POLL_MS = 20_000

/** Live counts straight from Gmail — how close to zero the inbox actually is,
 * and how much sits under each category label.
 *
 * Deliberately on its OWN 20s interval, NOT tied to `refreshKey` — that prop
 * is accepted but intentionally unused as a dependency. `refreshKey` bumps
 * every second while a run is active (RunProgress's poll), and each summary
 * refresh costs one Gmail labels().get() call per category; wiring the two
 * together would mean thousands of Gmail API calls over a long bulk archive
 * run. Approve/reject actions show up within one poll tick (<=20s), which is
 * the right trade for a "how close to zero am I" panel, not a live counter. */
export function InboxSummary(_props: { refreshKey: number }) {
  const [data, setData] = useState<InboxSummaryData | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let cancelled = false
    const load = () => {
      api
        .inboxSummary()
        .then(d => {
          if (!cancelled) {
            setData(d)
            setError(null)
          }
        })
        .catch(e => {
          if (!cancelled) setError(e)
        })
        .finally(() => {
          if (!cancelled) setLoading(false)
        })
    }
    load()
    const interval = setInterval(load, POLL_MS)
    return () => {
      cancelled = true
      clearInterval(interval)
    }
  }, [])

  if (loading && !data) {
    return (
      <div
        data-testid="inbox-summary"
        className="rounded-lg border border-gray-200 bg-white p-3 text-xs text-gray-500"
      >
        Loading inbox summary…
      </div>
    )
  }

  if (error) {
    const message =
      error instanceof ApiError
        ? error.message
        : 'Could not load the inbox summary right now.'
    return (
      <div
        data-testid="inbox-summary"
        className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800"
      >
        {message}
      </div>
    )
  }

  if (!data) return null

  return (
    <div
      data-testid="inbox-summary"
      className="rounded-lg border border-gray-200 bg-white p-3"
    >
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <p className="text-sm font-bold text-gray-900">
          <span data-testid="inbox-total-count">{data.inbox_total}</span> in your inbox
        </p>
        {data.needs_your_call > 0 ? (
          <p className="text-xs font-medium text-amber-700">
            {data.needs_your_call} awaiting your call
          </p>
        ) : null}
      </div>
      {data.categories.length > 0 ? (
        <ul className="mt-2 flex flex-wrap gap-1.5">
          {data.categories.map(c => (
            <li
              key={c.key}
              data-testid={`inbox-summary-category-${c.key}`}
              className="rounded-full bg-gray-100 px-2.5 py-1 text-[11px] font-medium text-gray-700"
              title={c.channel_label_name}
            >
              {c.name} · {c.count}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  )
}
