'use client'


/**
 * Pinned above all content (spec/ui.md §Global chrome). Shown red whenever
 * `settings.dry_run` is true; replaced by a green LIVE bar the moment the
 * user turns dry-run off in Settings.
 */
export function DryRunBanner({ dryRun = true }: { dryRun?: boolean }) {
  if (!dryRun) {
    return (
      <div
        role="status"
        data-testid="dry-run-banner"
        className="sticky top-0 z-50 w-full border-b-4 border-emerald-900 bg-emerald-600 px-4 py-2 text-center text-white shadow-md"
      >
        <p className="text-sm font-bold tracking-wide uppercase">
          <span aria-hidden="true" className="mr-1.5">
            ●
          </span>
          Live — actions will modify your Gmail
        </p>
        <p className="text-[11px] font-medium text-emerald-100">
          Dry run is off. Approving a cluster or thread now really archives and labels mail. Every
          action is logged with an undo token.
        </p>
      </div>
    )
  }

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
        Approving or rejecting records your intent in the database only. No message is archived,
        labelled, deleted or moved. Turn dry-run off in Settings to act for real.
      </p>
    </div>
  )
}

type NavItem = { label: string; description: string; href?: string }

const RAIL: NavItem[] = [
  { label: 'Triage', description: 'Triage history' },
  { label: 'Settings', description: 'VIP list, categories, priorities profile' },
  { label: 'Digest', description: 'Catch-up digest of what was archived', href: '/app/digest' },
]

export function LeftRail({
  onNavigate,
  active,
}: {
  onNavigate: (label: string) => void
  active: string
}) {
  return (
    <nav aria-label="Main" className="w-44 shrink-0 border-r border-gray-200 bg-white p-2">
      <ul className="space-y-1">
        {RAIL.map(item => {
          const isActive = active === item.label
          // External href nav items use an anchor tag
          if (item.href) {
            return (
              <li key={item.label}>
                <a
                  href={item.href}
                  aria-current={isActive ? 'page' : undefined}
                  className={`flex w-full items-center justify-between rounded px-2.5 py-1.5 text-left text-sm font-medium focus:ring-2 focus:ring-gray-400 focus:outline-none ${
                    isActive ? 'bg-gray-900 text-white' : 'text-gray-700 hover:bg-gray-100'
                  }`}
                >
                  <span>{item.label}</span>
                </a>
              </li>
            )
          }
          return (
            <li key={item.label}>
              <button
                type="button"
                onClick={() => onNavigate(item.label)}
                aria-current={isActive ? 'page' : undefined}
                className={`flex w-full items-center justify-between rounded px-2.5 py-1.5 text-left text-sm font-medium focus:ring-2 focus:ring-gray-400 focus:outline-none ${
                  isActive
                    ? 'bg-gray-900 text-white'
                    : 'text-gray-700 hover:bg-gray-100'
                }`}
              >
                <span>{item.label}</span>
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
