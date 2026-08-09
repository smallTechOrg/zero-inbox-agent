'use client'

import { isRunActive, type Run } from '@/lib/types'

/** Real progress only — the numbers come straight from GET /api/runs/{id}. */
export function RunProgress({
  run,
  onCancel,
  cancelling,
}: {
  run: Run
  onCancel: () => void
  cancelling: boolean
}) {
  const total = run.items_total ?? 0
  const decided = run.items_decided ?? 0
  const active = isRunActive(run.status)

  // `items_total` is written early in the run, but there is a brief window at
  // the start where the mailbox has not been listed yet. That window is shown
  // as an explicit "counting threads" state with an indeterminate bar, rather
  // than a misleading, frozen-looking "0 of ?".
  const counting = active && total === 0
  const pct = total > 0 ? Math.min(100, (decided / total) * 100) : 0

  return (
    <section
      data-testid="run-progress"
      aria-live="polite"
      aria-label="Triage run progress"
      className="rounded-lg border border-blue-200 bg-blue-50 p-3"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-sm font-semibold text-blue-950">
          {active ? 'Triage running' : `Run ${run.status}`} —{' '}
          {counting ? (
            <span>counting threads in your inbox…</span>
          ) : (
            <>
              <span data-testid="run-progress-counts">
                {decided} of {total} threads decided
              </span>{' '}
              <span className="font-mono text-xs font-normal text-blue-800">
                ({decided}/{total} · {Math.round(pct)}%)
              </span>
            </>
          )}
        </p>
        {active ? (
          <button
            type="button"
            onClick={onCancel}
            disabled={cancelling}
            className="rounded border border-blue-300 bg-white px-2.5 py-1 text-xs font-semibold text-blue-900 hover:bg-blue-100 focus:ring-2 focus:ring-blue-400 focus:outline-none disabled:opacity-50"
          >
            {cancelling ? 'Cancelling…' : 'Cancel run'}
          </button>
        ) : null}
      </div>

      <div
        role="progressbar"
        {...(counting
          ? {}
          : { 'aria-valuenow': Math.round(pct), 'aria-valuemin': 0, 'aria-valuemax': 100 })}
        aria-valuetext={counting ? 'Counting threads' : `${decided} of ${total} threads decided`}
        aria-label="Threads decided"
        className="mt-2 h-2 w-full overflow-hidden rounded-full bg-blue-200"
      >
        {counting ? (
          <div className="h-full w-1/3 animate-pulse rounded-full bg-blue-500 motion-reduce:animate-none" />
        ) : (
          <div
            data-testid="run-progress-bar"
            className="h-full bg-blue-600 transition-[width] duration-500 ease-out motion-reduce:transition-none"
            style={{ width: `${pct}%` }}
          />
        )}
      </div>

      {run.counts && run.counts.by_tier ? (
        <ul className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-blue-900">
          {Object.entries(run.counts.by_tier).map(([tier, count]) => (
            <li key={tier} className="flex items-center gap-1">
              <span className="font-medium">{tier.replace(/_/g, ' ')}</span>
              <span className="font-mono">{count}</span>
            </li>
          ))}
        </ul>
      ) : null}

      {run.counts && run.counts.needs_your_call ? (
        <p className="mt-1 text-xs text-amber-800">
          {run.counts.needs_your_call} need your call
        </p>
      ) : null}

      {run.status === 'failed' ? (
        <p
          role="alert"
          className="mt-2 rounded border border-red-300 bg-red-50 p-2 text-xs text-red-900"
        >
          This run failed partway — undecided threads were kept in your inbox and moved to Needs
          your call.
          {run.error_message ? ` (${run.error_message})` : ''}
        </p>
      ) : null}

      {run.status === 'cancelled' ? (
        <p role="status" className="mt-2 text-xs text-blue-900">
          You cancelled this run. Threads already decided are shown below; the rest were left
          untouched.
        </p>
      ) : null}
    </section>
  )
}
