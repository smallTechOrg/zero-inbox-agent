'use client'

import type { DecidedBy } from '@/lib/types'

const TIERS: Record<string, { text: string; className: string }> = {
  rule: { text: 'RULE', className: 'border-emerald-300 bg-emerald-50 text-emerald-900' },
  sender_history: {
    text: 'SENDER HISTORY',
    className: 'border-sky-300 bg-sky-50 text-sky-900',
  },
  llm: { text: 'LLM', className: 'border-violet-300 bg-violet-50 text-violet-900' },
  // The backend emits `llm_deep` (src/domain/enums.py::DecidedBy), not `deep_read`.
  llm_deep: { text: 'DEEP READ', className: 'border-indigo-300 bg-indigo-50 text-indigo-900' },
  reviewer: { text: 'REVIEWER', className: 'border-amber-300 bg-amber-50 text-amber-900' },
  error: { text: 'ERROR', className: 'border-rose-300 bg-rose-50 text-rose-900' },
}

/** Tier badges carry text, never colour alone (spec/ui.md accessibility). */
export function TierBadge({ decidedBy }: { decidedBy: DecidedBy }) {
  const tier = TIERS[decidedBy] ?? {
    text: String(decidedBy || 'UNKNOWN').toUpperCase().replace(/_/g, ' '),
    className: 'border-gray-300 bg-gray-100 text-gray-800',
  }
  return (
    <span
      data-testid="tier-badge"
      title={`Decided by: ${tier.text}`}
      className={`inline-flex shrink-0 items-center rounded border px-1.5 py-0.5 text-[10px] font-bold tracking-wide ${tier.className}`}
    >
      {tier.text}
    </span>
  )
}

export function ConfidenceBar({ value }: { value: number | null }) {
  const pct = value == null ? 0 : Math.max(0, Math.min(1, value)) * 100
  const label = value == null ? 'unknown' : `${Math.round(pct)}%`
  return (
    <span
      data-testid="decision-confidence"
      className="inline-flex items-center gap-1.5"
      title={`Confidence: ${label}`}
    >
      <span className="h-1.5 w-16 overflow-hidden rounded-full bg-gray-200">
        <span
          className={`block h-full ${pct >= 80 ? 'bg-emerald-500' : pct >= 55 ? 'bg-amber-500' : 'bg-rose-500'}`}
          style={{ width: `${pct}%` }}
        />
      </span>
      <span className="font-mono text-[11px] text-gray-600">{label}</span>
    </span>
  )
}

/**
 * The taxonomy is seeded, so every decision carries a category. It is always
 * rendered — a missing category is shown as an explicit "Uncategorised", never
 * silently hidden, so a taxonomy gap reads as data and not as a missing field.
 */
export function CategoryChip({ category }: { category: string | null }) {
  const known = Boolean(category)
  const text = category || 'Uncategorised'
  return (
    <span
      data-testid="decision-category"
      title={`Category: ${text}`}
      className={`inline-flex shrink-0 items-center gap-1 rounded border px-1.5 py-0.5 text-[11px] font-medium ${
        known
          ? 'border-teal-300 bg-teal-50 text-teal-900'
          : 'border-dashed border-gray-300 bg-gray-50 text-gray-500 italic'
      }`}
    >
      <span className="text-[9px] font-bold tracking-wide uppercase opacity-60">Cat</span>
      {text}
    </span>
  )
}

export function ActionChip({ action }: { action: string }) {
  const keep = /keep|inbox/i.test(action)
  return (
    <span
      className={`inline-flex shrink-0 items-center rounded border px-1.5 py-0.5 text-[11px] font-semibold ${
        keep
          ? 'border-emerald-300 bg-emerald-50 text-emerald-900'
          : 'border-gray-300 bg-gray-50 text-gray-800'
      }`}
    >
      {action.replace(/_/g, ' ')}
    </span>
  )
}
