'use client'

import type { ReactNode } from 'react'

/**
 * Stub convention (spec/ui.md): every not-yet-built surface renders its real
 * layout, greyed, with a `COMING SOON · Phase N` chip and a tooltip naming the
 * phase. Stub controls are `disabled` and never fire a request, so a stub can
 * never be mistaken for a broken feature.
 */

export function ComingSoonChip({ phase }: { phase: 2 | 3 }) {
  return (
    <span
      className="inline-flex shrink-0 items-center rounded border border-amber-300 bg-amber-50 px-1.5 py-0.5 text-[10px] font-semibold tracking-wide text-amber-800 uppercase"
      title={`Not built yet — planned for Phase ${phase}`}
    >
      Coming soon · Phase {phase}
    </span>
  )
}

export function StubButton({
  label,
  phase,
  className = '',
}: {
  label: string
  phase: 2 | 3
  className?: string
}) {
  const tip = `${label} is not built yet — planned for Phase ${phase}. This control is intentionally disabled.`
  return (
    <span title={tip} className="inline-flex items-center gap-1.5 opacity-50">
      <button
        type="button"
        disabled
        aria-disabled="true"
        aria-label={`${label} (coming soon, Phase ${phase})`}
        title={tip}
        className={`cursor-not-allowed rounded border border-gray-300 bg-white px-2 py-1 text-xs font-medium text-gray-600 ${className}`}
      >
        {label}
      </button>
      <ComingSoonChip phase={phase} />
    </span>
  )
}

export function StubPanel({
  title,
  phase,
  description,
  children,
}: {
  title: string
  phase: 2 | 3
  description: string
  children?: ReactNode
}) {
  return (
    <section
      aria-label={`${title} (coming soon, Phase ${phase})`}
      title={`${title} is not built yet — planned for Phase ${phase}.`}
      className="rounded-lg border border-dashed border-gray-300 bg-white p-3 opacity-50"
    >
      <div className="mb-1.5 flex items-center justify-between gap-2">
        <h3 className="text-sm font-semibold text-gray-800">{title}</h3>
        <ComingSoonChip phase={phase} />
      </div>
      <p className="text-xs leading-relaxed text-gray-600">{description}</p>
      {children ? <div className="mt-2 pointer-events-none">{children}</div> : null}
    </section>
  )
}
