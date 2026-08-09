'use client'

import { useCallback, useEffect, useState } from 'react'
import { api } from '@/lib/api'
import type { TriageItem } from '@/lib/types'
import { ErrorState, SkeletonRows } from './States'
import { ThreadRow } from './ThreadRow'

/**
 * Everything below the confidence floor. Attention-positive, not an error:
 * these threads stayed in the inbox on purpose.
 */
export function NeedsYourCall({
  runId,
  refreshKey,
  dryRun,
}: {
  runId: string
  refreshKey: number
  dryRun: boolean
}) {
  const [items, setItems] = useState<TriageItem[] | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<unknown>(null)
  const [busy, setBusy] = useState(false)
  const [actionLogIds, setActionLogIds] = useState<Record<string, string>>({})
  const [undoingId, setUndoingId] = useState<string | null>(null)
  const [undoResults, setUndoResults] = useState<Record<string, 'ok' | 'error'>>({})

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      setItems(await api.itemsByStatus(runId, 'needs_your_call'))
    } catch (e) {
      setError(e)
    } finally {
      setLoading(false)
    }
  }, [runId])

  useEffect(() => {
    void load()
  }, [load, refreshKey])

  const decide = useCallback(
    async (decisionId: string, status: 'approved' | 'rejected') => {
      setBusy(true)
      try {
        await api.decide(decisionId, status)
        setItems(prev =>
          prev ? prev.map(i => (i.decision_id === decisionId ? { ...i, status } : i)) : prev,
        )
        // Reject never mutates the mailbox, in any phase. Approve only applies
        // for real once dry-run is off.
        if (status === 'approved' && !dryRun) {
          try {
            const [result] = await api.applyDecisions([decisionId])
            if (result?.action_log_id) {
              setActionLogIds(prev => ({ ...prev, [decisionId]: result.action_log_id }))
              setItems(prev =>
                prev
                  ? prev.map(i => (i.decision_id === decisionId ? { ...i, status: 'applied' } : i))
                  : prev,
              )
            }
          } catch (e) {
            setError(e)
          }
        }
      } catch (e) {
        setError(e)
      } finally {
        setBusy(false)
      }
    },
    [dryRun],
  )

  const undo = useCallback(async (decisionId: string, actionLogId: string) => {
    setUndoingId(decisionId)
    try {
      await api.undoAction(actionLogId)
      setUndoResults(prev => ({ ...prev, [decisionId]: 'ok' }))
      setItems(prev =>
        prev ? prev.map(i => (i.decision_id === decisionId ? { ...i, status: 'undone' } : i)) : prev,
      )
    } catch {
      setUndoResults(prev => ({ ...prev, [decisionId]: 'error' }))
    } finally {
      setUndoingId(null)
    }
  }, [])

  return (
    <section
      data-testid="needs-your-call"
      aria-label="Needs your call"
      className="rounded-lg border border-amber-300 bg-amber-50 p-3"
    >
      <div className="mb-2 flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="text-sm font-bold tracking-wide text-amber-900 uppercase">
          Needs your call
        </h2>
        <p className="text-xs text-amber-900">
          Below the confidence floor — these stayed in your inbox on purpose.
        </p>
      </div>

      {loading ? (
        <SkeletonRows rows={2} label="Loading threads that need your call…" />
      ) : error ? (
        <ErrorState error={error} onRetry={() => void load()} />
      ) : items && items.length > 0 ? (
        <ul className="rounded border border-amber-200 bg-white">
          {items.map(item => (
            <ThreadRow
              key={item.decision_id}
              item={item}
              onDecide={decide}
              busy={busy}
              actionLogId={actionLogIds[item.decision_id] ?? null}
              onUndo={undo}
              undoing={undoingId === item.decision_id}
              undoResult={undoResults[item.decision_id] ?? null}
            />
          ))}
        </ul>
      ) : (
        <p className="rounded border border-amber-200 bg-white px-3 py-4 text-sm text-gray-700">
          Nothing needs your call from this run — every thread was decided with enough confidence.
        </p>
      )}
    </section>
  )
}
