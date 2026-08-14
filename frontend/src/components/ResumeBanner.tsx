'use client'

import { useState } from 'react'

/**
 * Screen 13 in spec/ui.md — the interrupted-run banner.
 *
 * Shown only when GET /api/runs/latest reports `status === "resumable"`: a run that
 * was interrupted with work already persisted. It is never a failure — every decided
 * thread is durable in the database, and resuming continues the SAME run without
 * re-classifying a single one of them.
 */

export type ResumableRun = {
  id: string
  items_total: number
  items_decided: number
}

type ResumeResult = {
  run_id: string
  items_total: number
  items_decided: number
  remaining: number
}

type Envelope<T> = { data: T | null; error: { code: string; message: string } | null }

async function postResume(runId: string): Promise<ResumeResult> {
  let res: Response
  try {
    res = await fetch(`/api/runs/${runId}/resume`, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
    })
  } catch {
    throw new Error('Could not reach the server — is it running on http://localhost:8001 ?')
  }
  let body: Envelope<ResumeResult> | null = null
  try {
    body = (await res.json()) as Envelope<ResumeResult>
  } catch {
    body = null
  }
  if (!res.ok || !body?.data) {
    throw new Error(body?.error?.message ?? `Resume failed (HTTP ${res.status})`)
  }
  return body.data
}

const fmt = (n: number) => n.toLocaleString()

export function ResumeBanner({
  run,
  onResumed,
  onStartFresh,
}: {
  run: ResumableRun
  onResumed: () => void
  onStartFresh?: () => void
}) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const done = run.items_decided ?? 0
  const total = Math.max(run.items_total ?? 0, done)

  const resume = async () => {
    setBusy(true)
    setError(null)
    try {
      await postResume(run.id)
      onResumed()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Resume failed')
    } finally {
      setBusy(false)
    }
  }

  const startFresh = () => {
    if (!onStartFresh) return
    const ok = window.confirm(
      `This re-classifies all ${fmt(total)} threads and costs more. Start a fresh run instead?`,
    )
    if (ok) onStartFresh()
  }

  return (
    <section
      data-testid="resume-banner"
      role="status"
      className="rounded-lg border border-amber-300 bg-amber-50 px-4 py-3"
    >
      <p className="text-sm font-semibold text-amber-900">
        Run interrupted — {fmt(done)} of {fmt(total)} threads already triaged.
      </p>
      <p className="mt-0.5 text-xs text-amber-800">
        Nothing was left half-applied. Resuming continues this same run and never
        re-classifies a thread it already decided.
      </p>

      {error ? (
        <p data-testid="resume-error" className="mt-2 text-xs font-medium text-red-700">
          {error}
        </p>
      ) : null}

      <div className="mt-2.5 flex flex-wrap items-center gap-3">
        <button
          type="button"
          data-testid="resume-run"
          onClick={() => void resume()}
          disabled={busy}
          className="inline-flex items-center gap-2 rounded-lg bg-amber-600 px-3.5 py-1.5 text-sm font-semibold text-white hover:bg-amber-700 focus:ring-2 focus:ring-amber-400 focus:outline-none disabled:opacity-60"
        >
          {busy ? (
            <>
              <span
                aria-hidden="true"
                className="h-3 w-3 animate-spin rounded-full border-2 border-white border-t-transparent"
              />
              Resuming…
            </>
          ) : (
            <>
              Resume run — {fmt(done)} of {fmt(total)} already done
            </>
          )}
        </button>

        {onStartFresh ? (
          <button
            type="button"
            data-testid="resume-start-fresh"
            onClick={startFresh}
            disabled={busy}
            className="text-xs font-medium text-amber-900 underline underline-offset-2 hover:text-amber-700 disabled:opacity-60"
          >
            Start a fresh run instead
          </button>
        ) : null}

        {error ? (
          <button
            type="button"
            data-testid="resume-retry"
            onClick={() => void resume()}
            disabled={busy}
            className="text-xs font-medium text-red-700 underline underline-offset-2"
          >
            Retry
          </button>
        ) : null}
      </div>
    </section>
  )
}

export default ResumeBanner
