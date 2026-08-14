'use client'

/**
 * The Inbox-Zero card — ui.md screen 16.
 *
 * The single honest answer to "how far from zero am I, and why?". It states the
 * inbox-zero *definition* verbatim and always visible (not a tooltip, not a
 * modal): a user who thinks "zero" means one thing and gets another has been
 * misled. A run that archived nothing must never render in the neutral state —
 * it renders the loud red apply-failure bar with a working Retry.
 *
 * Loaded from `GET /api/runs/{run_id}/remainder`, refetched on the three Phase-7
 * SSE events. It never polls.
 */

import { useCallback, useEffect, useMemo, useState } from 'react'
import { api } from '@/lib/api'
import {
  REMAINDER_ORDER,
  type Category,
  type RemainderBuckets,
  type RemainderLedger,
} from '@/lib/types'
import { useSse } from '@/lib/SseContext'
import { ErrorState, SkeletonRows } from '@/components/States'

/** The events that make the ledger stale. Refetch, never poll. */
const REFETCH_ON = ['apply_progress', 'inbox_zero_report', 'run_apply_failed']

const BUCKET_LABEL: Record<keyof RemainderBuckets, string> = {
  needs_your_call: 'need your call',
  category_keep: 'kept by category',
  below_threshold: 'not confident enough to archive on its own',
  held_by_never_miss: 'held by VIP or reply history',
  unclassified: 'decided before this policy existed',
}

const BUCKET_REASON: Record<keyof RemainderBuckets, string> = {
  needs_your_call: 'below the confidence floor, or the agent couldn’t decide',
  category_keep: '',
  below_threshold: '',
  held_by_never_miss: '',
  unclassified: '',
}

/** "A", "A and B", "A, B and C" — the taxonomy read back in the user's words. */
export function nameList(names: string[], fallback: string): string {
  if (names.length === 0) return fallback
  if (names.length === 1) return names[0]
  return `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`
}

export interface InboxZeroCardProps {
  /** The latest run, or null when the user has never run triage. */
  runId: string | null
  /** The current global autonomy bar, for the below-threshold hint. */
  autoActThreshold?: number | null
  /** The user's confidence floor — the LOWER bound of the below-threshold
   *  band. Passed in rather than hardcoded: ui.md #16 wants the real scored
   *  range, and a literal here is exactly the drift this card already shipped
   *  once with its category lists. */
  confidenceFloor?: number | null
}

export function InboxZeroCard({ runId, autoActThreshold, confidenceFloor }: InboxZeroCardProps) {
  const { events } = useSse()
  const [ledger, setLedger] = useState<RemainderLedger | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const [retrying, setRetrying] = useState(false)
  const [retryError, setRetryError] = useState<string | null>(null)
  const [categories, setCategories] = useState<Category[] | null>(null)

  const load = useCallback(async () => {
    if (!runId) return
    setLoading(true)
    setError(null)
    try {
      setLedger(await api.runs.remainder(runId))
    } catch (e) {
      setError(e)
    } finally {
      setLoading(false)
    }
  }, [runId])

  useEffect(() => {
    void load()
  }, [load])

  // Category names for the `category_keep` row are read from the live taxonomy,
  // never hardcoded — a user who renames "People" must see their own word.
  useEffect(() => {
    let cancelled = false
    void api.categories
      .list()
      .then(rows => {
        if (!cancelled) setCategories(rows)
      })
      .catch(() => {
        // The ledger is still fully useful without the names.
      })
    return () => {
      cancelled = true
    }
  }, [])

  // Refetch on the Phase-7 events, not on a timer.
  const refetchSignal = useMemo(
    () => events.filter(e => REFETCH_ON.includes(e.type)).length,
    [events],
  )
  useEffect(() => {
    if (refetchSignal > 0) void load()
  }, [refetchSignal, load])

  const retryArchiving = useCallback(async () => {
    if (!runId) return
    setRetrying(true)
    setRetryError(null)
    try {
      await api.runs.apply(runId)
      await load()
    } catch (e) {
      setRetryError(e instanceof Error ? e.message : 'Retry failed.')
    } finally {
      setRetrying(false)
    }
  }, [runId, load])

  // Both lists in the definition text are read from the LIVE taxonomy, never
  // hardcoded (ui.md #16). Hardcoding is how the card came to claim "Receipts
  // always stay" for a whole phase after Receipts became an archive category.
  const keepCategoryNames = (categories ?? [])
    .filter(c => c.default_action === 'keep')
    .map(c => c.name)
  const archiveCategoryNames = (categories ?? [])
    .filter(c => c.default_action === 'archive')
    .map(c => c.name)

  if (!runId) {
    return (
      <section
        aria-label="Inbox zero"
        data-testid="inbox-zero-card"
        className="rounded-xl border border-gray-200 bg-white p-4"
      >
        <p className="text-sm text-gray-600">Run triage to see how far you are from zero.</p>
      </section>
    )
  }

  if (loading && !ledger) {
    return <SkeletonRows rows={4} label="Loading your distance to inbox zero…" />
  }
  if (error && !ledger) {
    return <ErrorState error={error} onRetry={() => void load()} />
  }
  if (!ledger) return null

  // A dry run archiving nothing is the CORRECT outcome, not a failure. The
  // backend follows api.md literally (`apply_ok=false` when distance > 0, with
  // no dry-run exemption); the dry-run chip is the honest presentation of that,
  // and the red "could not archive" bar beside it would be a lie.
  const isDryRun = ledger.dry_run === true
  const applyFailed = !isDryRun && ledger.apply_ok === false
  const reason =
    ledger.apply_failed_reason ??
    `${ledger.distance_to_zero} archive${
      ledger.distance_to_zero === 1 ? '' : 's'
    } did not complete — see the action log.`

  return (
    <section
      aria-label="Inbox zero"
      data-testid="inbox-zero-card"
      data-apply-ok={String(ledger.apply_ok)}
      className="space-y-3 rounded-xl border border-gray-200 bg-white p-4 shadow-sm"
    >
      {/* The failure state is loud, not a toast — above the headline. */}
      {applyFailed ? (
        <div
          role="alert"
          data-testid="apply-failed-bar"
          className="rounded-lg border border-rose-300 bg-rose-50 p-3"
        >
          <p className="text-sm font-bold text-rose-900">
            The agent decided {ledger.distance_to_zero.toLocaleString()} thread
            {ledger.distance_to_zero === 1 ? '' : 's'} should leave your inbox but could not archive
            them.
          </p>
          <p data-testid="apply-failed-reason" className="mt-1 text-xs text-rose-800">
            {reason}
          </p>
          {/* Never reached in dry-run: `applyFailed` already excludes it. */}
          <button
            type="button"
            data-testid="retry-archiving"
            onClick={() => void retryArchiving()}
            disabled={retrying}
            className="mt-2 rounded-lg border border-rose-400 bg-white px-3 py-1.5 text-xs font-semibold text-rose-800 hover:bg-rose-100 focus:ring-2 focus:ring-rose-400 focus:outline-none disabled:opacity-50"
          >
            {retrying ? 'Retrying…' : 'Retry archiving'}
          </button>
          {retryError ? (
            <p className="mt-1 text-xs font-medium text-rose-900">{retryError}</p>
          ) : null}
        </div>
      ) : null}

      {/* Headline — three numbers, largest first */}
      <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1">
        <span data-testid="iz-applied" className="text-sm text-gray-900">
          <span className="text-xl font-bold">{ledger.applied.toLocaleString()}</span> archived this
          run
        </span>
        <span data-testid="iz-inbox-remaining" className="text-sm text-gray-700">
          <span className="font-bold">{ledger.inbox_remaining.toLocaleString()}</span> still in your
          inbox
        </span>
        <span data-testid="iz-distance" className="text-sm text-gray-700">
          distance to zero:{' '}
          <span className="font-bold">{ledger.distance_to_zero.toLocaleString()}</span>
        </span>
        {isDryRun ? (
          <span
            data-testid="iz-dry-run-chip"
            className="rounded border border-amber-400 bg-amber-50 px-1.5 py-0.5 text-[10px] font-bold text-amber-900"
          >
            DRY RUN — nothing was archived
          </span>
        ) : null}
      </div>

      {/* The definition — plainly stated, always visible */}
      <div
        data-testid="inbox-zero-definition"
        className="rounded-lg border border-gray-200 bg-gray-50 p-3 text-xs leading-relaxed text-gray-700"
      >
        <p className="font-bold text-gray-900">
          Inbox zero means your inbox holds only what needs a human.
        </p>
        <p className="mt-1">
          <span data-testid="iz-archive-categories">
            {nameList(archiveCategoryNames, 'The categories you set to archive')}
          </span>{' '}
          {archiveCategoryNames.length === 1 ? 'is' : 'are'} archived — never deleted, always
          undoable — when the agent is confident enough to act alone.{' '}
          <span data-testid="iz-keep-categories" className="font-semibold">
            {nameList(keepCategoryNames, 'The categories you set to keep')} always stay
          </span>
          , along with
          anyone you’ve replied to, anyone on your VIP list, anything time-sensitive, anything it
          wasn’t confident about, and anything one of your own rules kept.
        </p>
        <p className="mt-1 italic text-gray-500">Change which categories leave → Settings → Taxonomy</p>
      </div>

      {/* The remainder ledger — buckets at zero are greyed, never hidden */}
      <ul data-testid="remainder-ledger" className="divide-y divide-gray-100 rounded-lg border border-gray-200">
        {REMAINDER_ORDER.map(bucket => {
          const count = ledger.remainder?.[bucket] ?? 0
          // `unclassified` is the one bucket rendered only when > 0 (ui.md #16).
          if (bucket === 'unclassified' && count === 0) return null
          const zero = count === 0
          let copy = BUCKET_LABEL[bucket]
          if (bucket === 'category_keep' && keepCategoryNames.length > 0) {
            copy = `${copy} — ${keepCategoryNames.join(', ')}`
          } else if (BUCKET_REASON[bucket]) {
            copy = `${copy} — ${BUCKET_REASON[bucket]}`
          }
          return (
            <li
              key={bucket}
              data-testid={`remainder-row-${bucket}`}
              data-count={String(count)}
              className={`flex items-baseline gap-2 px-3 py-2 text-xs ${
                zero ? 'text-gray-400' : 'text-gray-800'
              }`}
            >
              <span className="font-bold tabular-nums">{count.toLocaleString()}</span>
              <span>{copy}</span>
              {bucket === 'below_threshold' && autoActThreshold ? (
                <span className="text-[11px] text-gray-500" data-testid="iz-below-threshold-hint">
                  (the bar is {autoActThreshold.toFixed(2)}
                  {confidenceFloor != null
                    ? `; these scored ${confidenceFloor.toFixed(2)}–${(autoActThreshold - 0.01).toFixed(2)}`
                    : ''}
                  )
                </span>
              ) : null}
            </li>
          )
        })}
      </ul>
    </section>
  )
}
