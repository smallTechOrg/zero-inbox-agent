'use client'

/** Shared primitives — one card, one badge, one spinner-with-label everywhere. */

export function Section({
  title,
  aside,
  children,
}: {
  title: string
  aside?: React.ReactNode
  children: React.ReactNode
}) {
  return (
    <section className="zi-card p-5">
      <div className="mb-4 flex items-center justify-between gap-3">
        <h2 className="zi-h2">{title}</h2>
        {aside}
      </div>
      {children}
    </section>
  )
}

/** Spinner is never alone — always carries its present-participle label. */
export function Loading({ label }: { label: string }) {
  return (
    <p className="zi-body flex items-center gap-2 text-zi-fg-muted" role="status">
      <span className="zi-spinner" aria-hidden />
      {label}
    </p>
  )
}

export function ErrorNote({
  message,
  onRetry,
  retryLabel = 'Try again',
}: {
  message: string
  onRetry?: () => void
  retryLabel?: string
}) {
  return (
    <div
      role="alert"
      className="flex flex-wrap items-center gap-3 rounded-zi-r-md border border-zi-danger/30 bg-zi-danger-bg px-4 py-3"
    >
      <p className="zi-body text-zi-danger">{message}</p>
      {onRetry && (
        <button type="button" className="zi-btn zi-btn-secondary" onClick={onRetry}>
          {retryLabel}
        </button>
      )}
    </div>
  )
}

/**
 * The BINDING Phase-2 stub badge (spec/ui.md § Stub convention). Byte-exact
 * text — Playwright asserts on it. A stub must never look like a bug.
 */
export function Phase2Badge() {
  return (
    <span
      data-testid="phase2-stub-badge"
      className="zi-caption inline-flex items-center gap-1.5 rounded-zi-r-sm bg-zi-info-bg px-2.5 py-1 text-zi-info"
    >
      Coming in Phase 2 — not yet functional
    </span>
  )
}
