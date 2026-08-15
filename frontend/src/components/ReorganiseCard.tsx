'use client'

/**
 * The re-organisation progress card — spec/ui.md screen 27.
 *
 * Renders in the MAIN column, above the live feed, and self-updates with **no
 * clicks**: it polls `GET /api/reorg/{job_id}` every 2 s while the job is
 * running (the spec's beat is "at least every 3 s"). The `reorg_progress` SSE
 * event exists too, but the shared SSE subscriber list lives in a file this
 * slice does not own, so the poll is what makes the card honest today — and a
 * poll cannot silently stop delivering while looking alive.
 *
 * Three things this card refuses to do:
 *  - hide a skip until the end (the skipped-by-reason table renders WHILE it runs),
 *  - render green when anything was skipped (that is amber `Partial`),
 *  - claim work is irreversible (undo is offered for the whole job as ONE call).
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import {
  ApiError,
  REORG_SKIP_REASONS,
  isReorgActive,
  phase9Api,
  skippedTotal,
  type ReorgLedger,
  type ReorgUndoResult,
} from '@/lib/types'
import { ErrorState, SkeletonRows } from '@/components/States'

/** Where the running job id is parked so it survives a Settings→Triage switch
 *  and a page reload — the user must never lose sight of a running job. */
export const REORG_JOB_KEY = 'zi_reorg_job_id'
const REORG_STARTED_EVENT = 'zi:reorg-started'

export function rememberReorgJob(jobId: string): void {
  try {
    window.localStorage.setItem(REORG_JOB_KEY, jobId)
  } catch {
    // Private mode: the in-memory handoff below still works for this session.
  }
  window.dispatchEvent(new CustomEvent<string>(REORG_STARTED_EVENT, { detail: jobId }))
}

export function forgetReorgJob(): void {
  try {
    window.localStorage.removeItem(REORG_JOB_KEY)
  } catch {
    // Nothing to clean up.
  }
}

export function readRememberedReorgJob(): string | null {
  try {
    return window.localStorage.getItem(REORG_JOB_KEY)
  } catch {
    return null
  }
}

/** Subscribe to "a re-organisation just started". Returns an unsubscribe fn. */
export function onReorgStarted(fn: (jobId: string) => void): () => void {
  const handler = (e: Event) => fn((e as CustomEvent<string>).detail)
  window.addEventListener(REORG_STARTED_EVENT, handler)
  return () => window.removeEventListener(REORG_STARTED_EVENT, handler)
}

const REASON_LABEL: Record<string, string> = {
  not_reviewed: 'not reviewed — never mutated without a reviewer verdict',
  no_category_fit: 'no category fitted this thread',
  gmail_error: 'Gmail refused or errored',
  already_correct: 'already filed correctly — nothing to change',
  dry_run: 'dry run — recorded, Gmail untouched',
  cancelled: 'cancelled before this thread was reached',
}

const STATUS_COPY: Record<string, string> = {
  running: 'Re-organising',
  completed: 'Re-organised',
  partial: 'Partial — finished, but some threads were skipped',
  cancelled: 'Cancelled',
  failed: 'Failed',
}

export interface ReorganiseCardProps {
  jobId: string
  /** Called when the job reaches a terminal state, and after a bulk undo. */
  onFinished?: (ledger: ReorgLedger) => void
  /** Called when the user dismisses a finished job's card. */
  onDismiss?: () => void
}

export function ReorganiseCard({ jobId, onFinished, onDismiss }: ReorganiseCardProps) {
  const [ledger, setLedger] = useState<ReorgLedger | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [lastUpdate, setLastUpdate] = useState<number>(0)
  const [now, setNow] = useState<number>(() => Date.now())
  const [cancelling, setCancelling] = useState(false)
  const [undoing, setUndoing] = useState(false)
  const [undoResult, setUndoResult] = useState<ReorgUndoResult | null>(null)
  const [undoError, setUndoError] = useState<string | null>(null)
  const finishedRef = useRef(false)

  const load = useCallback(async () => {
    try {
      const fresh = await phase9Api.reorg.ledger(jobId)
      setLedger(fresh)
      setError(null)
      setLastUpdate(Date.now())
      if (!isReorgActive(fresh.status) && !finishedRef.current) {
        finishedRef.current = true
        onFinished?.(fresh)
      }
    } catch (e) {
      setError(e)
    }
  }, [jobId, onFinished])

  // First read is immediate; after that it is a 2 s beat while the job runs.
  useEffect(() => {
    finishedRef.current = false
    void load()
  }, [load])

  useEffect(() => {
    if (ledger && !isReorgActive(ledger.status)) return
    const t = setInterval(() => void load(), 2000)
    return () => clearInterval(t)
  }, [ledger, load])

  // "Working but slow" and "stuck" must look different — so the age of the last
  // successful read is on screen, ticking, not implied.
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(t)
  }, [])

  const cancel = useCallback(async () => {
    setCancelling(true)
    try {
      setLedger(await phase9Api.reorg.cancel(jobId))
      setLastUpdate(Date.now())
    } catch (e) {
      setError(e)
    } finally {
      setCancelling(false)
    }
  }, [jobId])

  const undo = useCallback(async () => {
    const count = ledger?.done ?? 0
    const ok = window.confirm(
      `Undo the whole re-organisation?\n\nThis restores the exact label set every one of the ` +
        `${count.toLocaleString()} re-organised thread${count === 1 ? '' : 's'} had before the job ran. ` +
        `Nothing is deleted, and running this twice is safe.`,
    )
    if (!ok) return
    setUndoing(true)
    setUndoError(null)
    try {
      const res = await phase9Api.reorg.undo(jobId)
      setUndoResult(res)
      await load()
    } catch (e) {
      setUndoError(
        e instanceof ApiError ? `${e.code}: ${e.message}` : 'Undo failed — nothing was changed.',
      )
    } finally {
      setUndoing(false)
    }
  }, [jobId, ledger, load])

  if (!ledger && error) {
    return (
      <section aria-label="Re-organisation" data-testid="reorg-card" data-state="error" className="space-y-2">
        <ErrorState error={error} onRetry={() => void load()} />
        {onDismiss ? (
          <button
            type="button"
            data-testid="reorg-dismiss"
            onClick={onDismiss}
            className="rounded-lg border border-gray-300 bg-white px-3 py-1.5 text-xs font-medium text-gray-700 hover:bg-gray-100"
          >
            {error instanceof ApiError && error.status === 404
              ? 'That job no longer exists on the server — dismiss'
              : 'Dismiss'}
          </button>
        ) : null}
      </section>
    )
  }
  if (!ledger) {
    return (
      <section aria-label="Re-organisation" data-testid="reorg-card" data-state="loading">
        <SkeletonRows rows={3} label="Reading the re-organisation ledger…" />
      </section>
    )
  }

  const skipped = ledger.skipped ?? {}
  const skippedSum = skippedTotal(skipped)
  const active = isReorgActive(ledger.status)
  const anySkipped = skippedSum > 0
  // Amber whenever anything was skipped — a finished job that skipped work is
  // "Partial", never green and never "completed".
  const tone = ledger.status === 'failed' ? 'bad' : active ? 'busy' : anySkipped ? 'warn' : 'ok'
  const pct = ledger.total > 0 ? Math.min(100, Math.round(((ledger.done + skippedSum) / ledger.total) * 100)) : 0
  const staleFor = lastUpdate ? Math.max(0, Math.round((now - lastUpdate) / 1000)) : 0
  const stalled = active && staleFor >= 15

  const shell =
    tone === 'bad'
      ? 'border-rose-300 bg-rose-50'
      : tone === 'warn'
        ? 'border-amber-300 bg-amber-50'
        : tone === 'busy'
          ? 'border-sky-300 bg-sky-50'
          : 'border-emerald-300 bg-emerald-50'

  const headline =
    ledger.status === 'completed' && anySkipped
      ? STATUS_COPY.partial
      : (STATUS_COPY[ledger.status] ?? ledger.status)

  return (
    <section
      aria-label="Re-organisation"
      data-testid="reorg-card"
      data-status={ledger.status}
      data-state={tone}
      data-skipped-total={String(skippedSum)}
      className={`space-y-3 rounded-xl border p-4 shadow-sm ${shell}`}
    >
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <div>
          <p data-testid="reorg-headline" className="text-sm font-bold text-gray-900">
            {/* State is carried by the word, never by the colour alone. */}
            {anySkipped && !active ? (
              <span
                data-testid="reorg-partial-badge"
                className="mr-2 rounded border border-amber-500 bg-white px-1.5 py-0.5 text-[10px] font-bold tracking-wide text-amber-900 uppercase"
              >
                Partial
              </span>
            ) : null}
            {headline} your past decisions
          </p>
          <p className="mt-0.5 text-xs text-gray-700">
            Every past decision for this mailbox, including mail that is already archived. Nothing is
            deleted, and the whole job can be undone in one click.
          </p>
        </div>
        {ledger.dry_run ? (
          <span
            data-testid="reorg-dry-run-chip"
            className="rounded border border-amber-400 bg-white px-1.5 py-0.5 text-[10px] font-bold text-amber-900"
          >
            DRY RUN — Gmail is not being touched
          </span>
        ) : null}
      </div>

      {/* Progress — done / total, always with the raw numbers beside the bar. */}
      <div className="space-y-1">
        <div className="flex flex-wrap items-baseline justify-between gap-2 text-xs text-gray-800">
          <span data-testid="reorg-progress-count" className="font-bold tabular-nums">
            {ledger.done.toLocaleString()} / {ledger.total.toLocaleString()} re-organised
          </span>
          <span data-testid="reorg-progress-pct" className="tabular-nums">
            {pct}% accounted for
          </span>
        </div>
        <div
          className="h-2 w-full overflow-hidden rounded bg-white"
          role="progressbar"
          aria-valuemin={0}
          aria-valuemax={ledger.total}
          aria-valuenow={ledger.done}
          aria-label="Re-organisation progress"
        >
          <div
            className={`h-full transition-all ${tone === 'bad' ? 'bg-rose-500' : anySkipped ? 'bg-amber-500' : 'bg-emerald-500'}`}
            style={{ width: `${pct}%` }}
          />
        </div>
        <p data-testid="reorg-phase" className="text-[11px] text-gray-600">
          {active ? (
            <>
              {ledger.phase ? `Phase: ${ledger.phase}` : 'Working through your decisions'}
              {ledger.current_category ? ` · ${ledger.current_category}` : ''} ·{' '}
              <span data-testid="reorg-last-update">
                last update {staleFor}s ago
              </span>
            </>
          ) : (
            `Finished · ${ledger.done.toLocaleString()} re-organised · ${skippedSum.toLocaleString()} skipped`
          )}
        </p>
        {stalled ? (
          <p
            role="alert"
            data-testid="reorg-stalled"
            className="rounded border border-amber-400 bg-white px-2 py-1 text-[11px] font-semibold text-amber-900"
          >
            No update for {staleFor}s — the job may be stuck. Nothing has been lost: cancel and the
            work already done stays done, and stays undoable.
          </p>
        ) : null}
      </div>

      {/* The skipped-by-reason table — rendered WHILE the job runs. */}
      <div>
        <p data-testid="reorg-skipped-heading" className="text-xs font-bold text-gray-900">
          {anySkipped
            ? `${skippedSum.toLocaleString()} thread${skippedSum === 1 ? '' : 's'} it could not re-organise, by reason`
            : 'Nothing skipped so far'}
        </p>
        <ul
          data-testid="reorg-skipped-table"
          className="mt-1 divide-y divide-white/70 rounded-lg border border-white bg-white/60"
        >
          {REORG_SKIP_REASONS.map(reason => {
            const n = skipped[reason] ?? 0
            return (
              <li
                key={reason}
                data-testid={`reorg-skip-${reason}`}
                data-count={String(n)}
                data-state={n === 0 ? 'empty' : 'skipped'}
                className={`flex items-baseline gap-2 px-3 py-1.5 text-[11px] ${n === 0 ? 'text-gray-400' : 'text-gray-900'}`}
              >
                <span className="w-14 shrink-0 font-bold tabular-nums">
                  {n === 0 ? 'none' : n.toLocaleString()}
                </span>
                <span>{REASON_LABEL[reason] ?? reason}</span>
              </li>
            )
          })}
          {/* A reason the backend invented that this build does not know about
              is still shown — "unknown" is never a synonym for "hidden". */}
          {Object.keys(skipped)
            .filter(r => !(REORG_SKIP_REASONS as readonly string[]).includes(r))
            .map(r => (
              <li
                key={r}
                data-testid={`reorg-skip-${r}`}
                data-count={String(skipped[r])}
                className="flex items-baseline gap-2 px-3 py-1.5 text-[11px] text-gray-900"
              >
                <span className="w-14 shrink-0 font-bold tabular-nums">
                  {(skipped[r] ?? 0).toLocaleString()}
                </span>
                <span>{r}</span>
              </li>
            ))}
        </ul>
      </div>

      {ledger.error_message ? (
        <p
          role="alert"
          data-testid="reorg-error-message"
          className="rounded border border-rose-300 bg-white px-2 py-1.5 text-[11px] font-semibold text-rose-900"
        >
          {ledger.error_message}
        </p>
      ) : null}

      {error ? (
        <p data-testid="reorg-poll-error" className="text-[11px] font-medium text-rose-800">
          Could not read the latest progress ({error instanceof ApiError ? error.code : 'error'}) —
          retrying every 2s. The job itself is unaffected.
        </p>
      ) : null}

      <div className="flex flex-wrap items-center gap-2">
        {active ? (
          <button
            type="button"
            data-testid="reorg-cancel"
            onClick={() => void cancel()}
            disabled={cancelling}
            title="Stop here. Work already done stays done — and stays undoable."
            className="rounded-lg border border-gray-400 bg-white px-3 py-1.5 text-xs font-semibold text-gray-800 hover:bg-gray-100 focus:ring-2 focus:ring-gray-500 focus:outline-none disabled:opacity-50"
          >
            {cancelling ? 'Cancelling…' : 'Cancel'}
          </button>
        ) : null}

        <button
          type="button"
          data-testid="reorg-undo"
          onClick={() => void undo()}
          disabled={undoing || ledger.undoable === false}
          title={
            ledger.undoable === false
              ? 'This job mutated nothing, so there is nothing to reverse.'
              : 'Restores every re-organised thread to its exact previous labels, in one operation.'
          }
          className="rounded-lg bg-gray-900 px-3 py-1.5 text-xs font-semibold text-white hover:bg-gray-700 focus:ring-2 focus:ring-gray-500 focus:outline-none disabled:opacity-50"
        >
          {undoing ? 'Undoing the whole re-organisation…' : 'Undo the whole re-organisation'}
        </button>

        {!active && onDismiss ? (
          <button
            type="button"
            data-testid="reorg-dismiss"
            onClick={onDismiss}
            className="rounded-lg border border-gray-300 bg-white px-3 py-1.5 text-xs font-medium text-gray-700 hover:bg-gray-100"
          >
            Dismiss
          </button>
        ) : null}
      </div>

      {undoResult ? (
        <p data-testid="reorg-undo-result" className="text-xs font-semibold text-gray-900">
          {undoResult.reversed.toLocaleString()} thread
          {undoResult.reversed === 1 ? '' : 's'} restored to their previous labels
          {undoResult.already_undone > 0
            ? ` · ${undoResult.already_undone.toLocaleString()} were already back`
            : ''}
          {undoResult.failed?.length
            ? ` · ${undoResult.failed.length.toLocaleString()} could not be restored: ${undoResult.failed
                .slice(0, 3)
                .map(f => f.reason)
                .join(', ')}`
            : ''}
          .
        </p>
      ) : null}
      {undoError ? (
        <p data-testid="reorg-undo-error" className="text-xs font-semibold text-rose-800">
          {undoError}
        </p>
      ) : null}
    </section>
  )
}

export default ReorganiseCard
