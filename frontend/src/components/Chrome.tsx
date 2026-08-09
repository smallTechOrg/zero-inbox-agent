'use client'

import { ComingSoonChip } from './Stub'

/** Phase 1: permanent, unmissable, pinned above all content. */
export function DryRunBanner() {
  return (
    <div
      role="status"
      data-testid="dry-run-banner"
      className="sticky top-0 z-50 w-full border-b-4 border-red-900 bg-red-600 px-4 py-2 text-center text-white shadow-md"
    >
      <p className="text-sm font-bold tracking-wide uppercase">
        <span aria-hidden="true" className="mr-1.5">
          ●
        </span>
        Dry run — nothing in your Gmail has been changed
      </p>
      <p className="text-[11px] font-medium text-red-100">
        Phase 1 records your approvals and rejections in the database only. No message is
        archived, labelled, deleted or moved.
      </p>
    </div>
  )
}

type RailItem = { label: string; phase?: 2 | 3; description: string }

const RAIL: RailItem[] = [
  { label: 'Triage', description: 'Review clustered triage decisions' },
  { label: 'Rules', phase: 3, description: 'Proposed filter rules ranked by coverage' },
  { label: 'Chat', phase: 3, description: 'Turn plain English into rules' },
  { label: 'Digest', phase: 3, description: 'Daily summary of what was hidden' },
  { label: 'Backlog', phase: 3, description: 'Historical cleanup in dated chunks' },
  { label: 'Cost', phase: 3, description: 'Spend and rules-vs-LLM ratio' },
  { label: 'Settings', phase: 2, description: 'Thresholds, VIP list, priorities profile' },
]

export function LeftRail() {
  return (
    <nav aria-label="Main" className="w-44 shrink-0 border-r border-gray-200 bg-white p-2">
      <ul className="space-y-1">
        {RAIL.map(item => {
          if (!item.phase) {
            return (
              <li key={item.label}>
                <a
                  href="#triage-queue"
                  aria-current="page"
                  className="block rounded bg-gray-900 px-2.5 py-1.5 text-sm font-semibold text-white focus:ring-2 focus:ring-gray-400 focus:outline-none"
                >
                  {item.label}
                </a>
              </li>
            )
          }
          const tip = `${item.label} — ${item.description}. Not built yet; planned for Phase ${item.phase}.`
          return (
            <li key={item.label}>
              <button
                type="button"
                disabled
                aria-disabled="true"
                title={tip}
                aria-label={`${item.label} (coming soon, Phase ${item.phase})`}
                className="flex w-full cursor-not-allowed flex-col items-start gap-1 rounded px-2.5 py-1.5 text-left text-sm text-gray-600 opacity-50"
              >
                <span>{item.label}</span>
                <ComingSoonChip phase={item.phase} />
              </button>
            </li>
          )
        })}
      </ul>
    </nav>
  )
}

export function StatusPill({
  tone,
  children,
}: {
  tone: 'idle' | 'running' | 'ok' | 'bad'
  children: React.ReactNode
}) {
  const map = {
    idle: 'border-gray-300 bg-gray-100 text-gray-700',
    running: 'border-blue-300 bg-blue-50 text-blue-900',
    ok: 'border-emerald-300 bg-emerald-50 text-emerald-900',
    bad: 'border-rose-300 bg-rose-50 text-rose-900',
  } as const
  return (
    <span
      data-testid="run-status-pill"
      className={`inline-flex items-center rounded-full border px-2.5 py-0.5 text-xs font-semibold ${map[tone]}`}
    >
      {children}
    </span>
  )
}
