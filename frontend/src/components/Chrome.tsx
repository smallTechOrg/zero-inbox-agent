'use client'

/**
 * Global chrome — spec/ui.md § Global chrome, restyled onto the Phase 8 design
 * system. Behaviour is unchanged: this is a token migration plus the responsive
 * rail, not a rewrite of anything the console depends on.
 *
 * Every state surface below carries TEXT naming the state. That is the binding
 * token-level rule: a bar, pill or dot that means something and says nothing is
 * a defect, however unambiguous its colour looks to the person who wrote it.
 */

/**
 * Pinned above all content. Amber (`warn`) whenever `settings.dry_run` is true;
 * replaced by a green (`ok`) LIVE bar the moment dry-run is turned off in
 * Settings. Both states are labelled in words.
 */
export function DryRunBanner({ dryRun = true }: { dryRun?: boolean }) {
  if (!dryRun) {
    return (
      <div
        role="status"
        data-testid="dry-run-banner"
        className="sticky top-0 z-50 w-full border-b border-zi-ok bg-zi-ok px-4 py-2 text-center text-white"
      >
        <p className="zi-h3 tracking-wide uppercase">
          Live — actions will modify your Gmail
        </p>
        <p className="zi-caption text-white/90">
          Dry run is off. The agent really archives and labels mail. Every action is logged with an
          undo token, and nothing is ever deleted.
        </p>
      </div>
    )
  }

  return (
    <div
      role="status"
      data-testid="dry-run-banner"
      className="sticky top-0 z-50 w-full border-b border-zi-warn bg-zi-warn px-4 py-2 text-center text-white"
    >
      <p className="zi-h3 tracking-wide uppercase">
        Dry run — Gmail mutations are suppressed
      </p>
      <p className="zi-caption text-white/90">
        Decisions are recorded in the database only. No message is archived, labelled, deleted or
        moved. Turn dry-run off in Settings to act for real.
      </p>
    </div>
  )
}

type NavItem = { label: string; description: string; href?: string }

const RAIL: NavItem[] = [
  { label: 'Triage', description: 'Triage history' },
  { label: 'Settings', description: 'Account, VIP list, categories, priorities profile' },
  { label: 'Digest', description: 'Catch-up digest of what was archived', href: '/app/digest' },
]

/**
 * The rail. At ≥ 768px it is a vertical left rail; below that it collapses to a
 * horizontal scrolling tab strip pinned under the top bar (spec/ui.md §
 * Responsive layout). The rail collapses — the live feed and the Inbox-Zero
 * card never do.
 */
export function LeftRail({
  onNavigate,
  active,
}: {
  onNavigate: (label: string) => void
  active: string
}) {
  const itemClass = (isActive: boolean) =>
    `zi-focusable zi-body flex shrink-0 items-center rounded-zi-r-sm px-3 py-1.5 text-left font-medium no-underline transition-colors ${
      isActive
        ? 'bg-zi-bg-inverse text-white'
        : 'text-zi-fg-muted hover:bg-zi-bg-subtle hover:text-zi-fg'
    }`

  return (
    <nav
      aria-label="Main"
      className="shrink-0 border-zi-border bg-zi-bg max-md:w-full max-md:overflow-x-auto max-md:border-b md:w-44 md:border-r md:p-2"
    >
      <ul className="flex gap-1 p-2 max-md:flex-nowrap md:flex-col md:p-0">
        {RAIL.map(item => {
          const isActive = active === item.label
          return (
            <li key={item.label}>
              {item.href ? (
                <a
                  href={item.href}
                  title={item.description}
                  aria-current={isActive ? 'page' : undefined}
                  className={itemClass(isActive)}
                >
                  {item.label}
                </a>
              ) : (
                <button
                  type="button"
                  title={item.description}
                  onClick={() => onNavigate(item.label)}
                  aria-current={isActive ? 'page' : undefined}
                  className={`${itemClass(isActive)} w-full`}
                >
                  {item.label}
                </button>
              )}
            </li>
          )
        })}
      </ul>
    </nav>
  )
}

/** The run-status pill. `children` is always the state in words — the colour
 *  is a second channel, never the only one. */
export function StatusPill({
  tone,
  children,
}: {
  tone: 'idle' | 'running' | 'ok' | 'bad'
  children: React.ReactNode
}) {
  const map = {
    idle: 'border-zi-border bg-zi-bg-subtle text-zi-fg-muted',
    running: 'border-zi-info bg-zi-info-bg text-zi-info',
    ok: 'border-zi-ok bg-zi-ok-bg text-zi-ok',
    bad: 'border-zi-danger bg-zi-danger-bg text-zi-danger',
  } as const
  return (
    <span
      data-testid="run-status-pill"
      className={`zi-caption inline-flex items-center rounded-full border px-2.5 py-0.5 font-semibold ${map[tone]}`}
    >
      {children}
    </span>
  )
}
