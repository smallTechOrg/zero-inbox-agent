'use client'

import { useState } from 'react'
import type { TriageItem } from '@/lib/types'
import { ActionChip, CategoryChip, ConfidenceBar, TierBadge } from './TierBadge'
import { StubButton } from './Stub'

function formatDate(value: string | null) {
  if (!value) return ''
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return value
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' })
}

export function ThreadRow({
  item,
  onDecide,
  busy,
}: {
  item: TriageItem
  onDecide: (decisionId: string, status: 'approved' | 'rejected') => void
  busy: boolean
}) {
  const [open, setOpen] = useState(false)
  const decided = item.status === 'approved' || item.status === 'rejected'
  const sender = item.item.from_name || item.item.from_email || 'Unknown sender'

  return (
    <li className="border-t border-gray-100" data-testid="thread-row">
      <div className="flex items-start gap-3 px-3 py-2 hover:bg-gray-50">
        <button
          type="button"
          onClick={() => setOpen(o => !o)}
          aria-expanded={open}
          className="mt-0.5 w-4 shrink-0 font-mono text-xs text-gray-500 focus:ring-2 focus:ring-gray-400 focus:outline-none"
          aria-label={open ? 'Collapse thread details' : 'Expand thread details'}
        >
          {open ? '−' : '+'}
        </button>

        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span
              className={`truncate text-sm ${item.item.is_unread ? 'font-semibold text-gray-900' : 'text-gray-800'}`}
            >
              {item.item.subject || '(no subject)'}
            </span>
            <TierBadge decidedBy={item.decided_by} />
            <CategoryChip category={item.category} />
            <ActionChip action={item.proposed_action} />
            {item.time_sensitive ? (
              <span className="rounded border border-amber-300 bg-amber-50 px-1.5 py-0.5 text-[10px] font-bold text-amber-900">
                TIME SENSITIVE
              </span>
            ) : null}
          </div>
          <div className="mt-0.5 flex flex-wrap items-center gap-2 text-xs text-gray-600">
            <span className="truncate">{sender}</span>
            {item.item.from_email && item.item.from_name ? (
              <span className="truncate text-gray-400">{item.item.from_email}</span>
            ) : null}
            <span>{formatDate(item.item.internal_date)}</span>
            {item.item.message_count ? <span>{item.item.message_count} messages</span> : null}
            <ConfidenceBar value={item.confidence} />
          </div>
          {item.item.snippet_redacted ? (
            <p className="mt-0.5 truncate text-xs text-gray-500">{item.item.snippet_redacted}</p>
          ) : null}

          {open ? (
            <div
              data-testid="thread-detail"
              className="mt-2 rounded border border-gray-200 bg-gray-50 p-2.5"
            >
              <p className="text-[11px] font-semibold tracking-wide text-gray-500 uppercase">
                Reasoning
              </p>
              <p
                data-testid="decision-reasoning"
                className="mt-1 text-sm whitespace-pre-wrap text-gray-800"
              >
                {item.reasoning || 'No reasoning was recorded for this decision.'}
              </p>
              <dl className="mt-2 flex flex-wrap gap-x-6 gap-y-1 text-xs text-gray-600">
                <div className="flex gap-1.5">
                  <dt className="font-medium">Decided by</dt>
                  <dd className="font-mono">{item.decided_by}</dd>
                </div>
                <div className="flex gap-1.5">
                  <dt className="font-medium">Category</dt>
                  <dd className="font-mono">{item.category ?? 'uncategorised'}</dd>
                </div>
                {item.rule_name ? (
                  <div className="flex gap-1.5">
                    <dt className="font-medium">Rule</dt>
                    <dd className="font-mono">{item.rule_name}</dd>
                  </div>
                ) : null}
                <div className="flex gap-1.5">
                  <dt className="font-medium">Status</dt>
                  <dd className="font-mono">{item.status}</dd>
                </div>
              </dl>
              <div className="mt-2.5 flex flex-wrap items-center gap-2">
                <StubButton label="Undo" phase={2} />
                <StubButton label="Create Gmail filter" phase={3} />
                <StubButton label="Draft reply" phase={3} />
              </div>
            </div>
          ) : null}
        </div>

        <div className="flex shrink-0 items-center gap-1.5">
          {decided ? (
            <span
              className={`rounded border px-2 py-1 text-xs font-semibold ${
                item.status === 'approved'
                  ? 'border-emerald-300 bg-emerald-50 text-emerald-900'
                  : 'border-gray-300 bg-gray-100 text-gray-700'
              }`}
            >
              {item.status === 'approved' ? 'Approved' : 'Rejected'}
            </span>
          ) : (
            <>
              <button
                type="button"
                disabled={busy}
                onClick={() => onDecide(item.decision_id, 'approved')}
                className="rounded bg-gray-900 px-2.5 py-1 text-xs font-semibold text-white hover:bg-gray-700 focus:ring-2 focus:ring-gray-400 focus:outline-none disabled:opacity-50"
              >
                Approve
              </button>
              <button
                type="button"
                disabled={busy}
                onClick={() => onDecide(item.decision_id, 'rejected')}
                className="rounded border border-gray-300 bg-white px-2.5 py-1 text-xs font-semibold text-gray-800 hover:bg-gray-100 focus:ring-2 focus:ring-gray-400 focus:outline-none disabled:opacity-50"
              >
                Reject
              </button>
            </>
          )}
        </div>
      </div>
    </li>
  )
}
