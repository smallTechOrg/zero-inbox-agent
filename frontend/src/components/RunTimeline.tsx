'use client'

import { useEffect, useState } from 'react'
import { api, ApiError } from '../lib/api'
import type { Run } from '../lib/types'
import { useRunFeed } from '../lib/useRunFeed'
import { ErrorNote, Loading, Section } from './ui'

const STATUS_STYLE: Record<Run['status'], string> = {
  running: 'bg-zi-info-bg text-zi-info',
  completed: 'bg-zi-ok-bg text-zi-ok',
  interrupted: 'bg-zi-warn-bg text-zi-warn',
  undone: 'bg-zi-bg-subtle text-zi-fg-muted',
}

function when(iso: string | null): string {
  if (!iso) return '—'
  try {
    return new Date(iso).toLocaleString(undefined, {
      month: 'short',
      day: 'numeric',
      hour: 'numeric',
      minute: '2-digit',
    })
  } catch {
    return iso
  }
}

function RunCard({ run, onChanged }: { run: Run; onChanged: () => void }) {
  const [confirming, setConfirming] = useState(false)
  const [undoing, setUndoing] = useState(false)
  const [undoError, setUndoError] = useState<string | null>(null)
  // Undo streams on the same events channel — armed only while undoing.
  const undoFeed = useRunFeed(undoing ? run.id : null)

  const undoEvents = undoFeed.events.filter((e) => e.type.startsWith('undo'))
  const undoTerminal = undoFeed.terminalType
  useEffect(() => {
    // Terminal undo event arrived — let the parent refresh the card list.
    if (undoing && undoTerminal) {
      setUndoing(false)
      onChanged()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [undoTerminal])

  const startUndo = async () => {
    setConfirming(false)
    setUndoError(null)
    try {
      await api.runs.undo(run.id)
      setUndoing(true)
    } catch (e) {
      if (e instanceof ApiError) setUndoError(e.message)
      else setUndoError('Undo could not start — try again.')
    }
  }

  const canUndo = run.status === 'completed' || run.status === 'interrupted'

  return (
    <article className="zi-card p-4" data-testid="run-card">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-3">
          <p className="zi-h3">{when(run.started_at)}</p>
          <span className={`zi-caption rounded-zi-r-sm px-2 py-0.5 ${STATUS_STYLE[run.status]}`}>
            {run.status}
          </span>
        </div>
        {canUndo &&
          (confirming ? (
            <span className="flex items-center gap-2">
              <span className="zi-caption text-zi-fg-muted">
                Revert all {run.threads_decided} threads in Gmail?
              </span>
              <button type="button" className="zi-btn zi-btn-danger" onClick={startUndo}>
                Yes, undo it
              </button>
              <button
                type="button"
                className="zi-btn zi-btn-secondary"
                onClick={() => setConfirming(false)}
              >
                Keep it
              </button>
            </span>
          ) : (
            <button
              type="button"
              className="zi-btn zi-btn-secondary"
              disabled={undoing}
              title={undoing ? 'Undo in progress' : undefined}
              onClick={() => setConfirming(true)}
            >
              {undoing && <span className="zi-spinner" aria-hidden />}
              {undoing ? 'Undoing…' : 'Undo this run'}
            </button>
          ))}
      </div>

      <p className="zi-body mt-2 text-zi-fg-muted">
        {run.threads_decided} threads · {run.llm_calls} LLM calls ·{' '}
        {(run.tokens_in + run.tokens_out).toLocaleString()} tokens · $
        {run.est_cost_usd.toFixed(4)}
        {run.fallback_events > 0 && (
          <span className="text-zi-warn"> · {run.fallback_events} fallback events</span>
        )}
      </p>

      {Object.keys(run.counts).length > 0 && (
        <ul className="mt-2 flex flex-wrap gap-2">
          {Object.entries(run.counts).map(([name, count]) => (
            <li
              key={name}
              className="zi-caption rounded-zi-r-sm border border-zi-border bg-zi-bg-subtle px-2 py-0.5 text-zi-fg-muted"
            >
              {name} <span className="zi-num font-semibold text-zi-fg">{count}</span>
            </li>
          ))}
        </ul>
      )}

      {run.status === 'interrupted' && run.interrupt_reason && (
        <p className="zi-body mt-2 rounded-zi-r-sm bg-zi-warn-bg px-3 py-2 text-zi-warn">
          Interrupted: {run.interrupt_reason} Pressing &ldquo;Resume cleaning&rdquo; picks up
          exactly where it stopped.
        </p>
      )}

      {undoError && (
        <div className="mt-2">
          <ErrorNote message={undoError} onRetry={startUndo} retryLabel="Retry undo" />
        </div>
      )}

      {/* Undo progress streams into the card (spec/ui.md §4). */}
      {undoing && (
        <div className="mt-3 border-t border-zi-border pt-2">
          {undoEvents.length === 0 ? (
            <Loading label="Undoing this run…" />
          ) : (
            <ul className="zi-body max-h-40 space-y-1 overflow-y-auto text-zi-fg-muted">
              {[...undoEvents].reverse().map((e) => (
                <li key={e.seq} className="zi-row-enter">
                  {e.sentence}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </article>
  )
}

/** Run timeline (spec/ui.md §4) — past runs, newest first. */
export function RunTimeline({ runs, onChanged }: { runs: Run[]; onChanged: () => void }) {
  return (
    <Section title="Past runs">
      {runs.length === 0 ? (
        <p className="zi-body text-zi-fg-muted">
          No runs yet. Press &ldquo;Clean my inbox&rdquo; above to clean your first 50-thread
          chunk — every run lands here with a one-click undo.
        </p>
      ) : (
        <div className="space-y-3">
          {runs.map((run) => (
            <RunCard key={run.id} run={run} onChanged={onChanged} />
          ))}
        </div>
      )}
    </Section>
  )
}
