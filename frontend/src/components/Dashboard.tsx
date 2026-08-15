'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import { api, ApiError, isGmailReconnect, isSignedOut } from '../lib/api'
import type { AuditSnapshot, Category, Me, Run } from '../lib/types'
import { useRunFeed } from '../lib/useRunFeed'
import { ActivityFeed } from './ActivityFeed'
import { CommandStrip } from './CommandStrip'
import { Header } from './Header'
import { CostsStub, LedgerStub, ProfilesStub } from './Phase2Stubs'
import { RunTimeline } from './RunTimeline'
import { TaxonomyPanel } from './TaxonomyPanel'
import { ErrorNote } from './ui'

/**
 * The one-surface signed-in dashboard (spec/ui.md), top to bottom:
 * header → command strip → live feed → run timeline → taxonomy → Phase-2 stubs.
 */
export function Dashboard({ me, onSignedOut }: { me: Me; onSignedOut: () => void }) {
  const [needsReconnect, setNeedsReconnect] = useState(me.gmail_status === 'needs_reconnect')

  const [audit, setAudit] = useState<AuditSnapshot | null>(null)
  const [auditState, setAuditState] = useState<'loading' | 'ready' | 'error'>('loading')

  const [categories, setCategories] = useState<Category[]>([])
  const [taxonomyLoading, setTaxonomyLoading] = useState(true)
  const [taxonomyError, setTaxonomyError] = useState<string | null>(null)

  const [runs, setRuns] = useState<Run[]>([])
  const [runsError, setRunsError] = useState<string | null>(null)

  const [activeRunId, setActiveRunId] = useState<string | null>(null)
  const [starting, setStarting] = useState(false)
  const [startError, setStartError] = useState<string | null>(null)

  const auditedOnce = useRef(false)

  /** Route every API failure through one policy: signed-out → front door,
   *  gmail_reconnect → the banner (the only token-error surface). */
  const handleFailure = useCallback(
    (e: unknown): string => {
      if (isSignedOut(e)) {
        onSignedOut()
        return 'Signed out.'
      }
      if (isGmailReconnect(e)) {
        setNeedsReconnect(true)
        return 'Reconnect Gmail to continue.'
      }
      return e instanceof ApiError ? e.message : 'Something went wrong — try again.'
    },
    [onSignedOut],
  )

  const refreshRuns = useCallback(async () => {
    try {
      const list = await api.runs.list()
      setRuns(list)
      setRunsError(null)
      // Reattach to an in-flight run after a reload (reconnect-safe feed).
      const running = list.find((r) => r.status === 'running')
      setActiveRunId((cur) => cur ?? running?.id ?? null)
    } catch (e) {
      setRunsError(handleFailure(e))
    }
  }, [handleFailure])

  const refreshTaxonomy = useCallback(async () => {
    setTaxonomyError(null)
    try {
      setCategories(await api.taxonomy.list())
    } catch (e) {
      setTaxonomyError(handleFailure(e))
    } finally {
      setTaxonomyLoading(false)
    }
  }, [handleFailure])

  const runAudit = useCallback(async () => {
    setAuditState('loading')
    try {
      setAudit(await api.runAudit())
      setAuditState('ready')
    } catch (e) {
      handleFailure(e)
      setAuditState('error')
    }
  }, [handleFailure])

  // First load: taxonomy + runs + latest audit; first visit auto-audits.
  useEffect(() => {
    void refreshTaxonomy()
    void refreshRuns()
    ;(async () => {
      try {
        const latest = await api.latestAudit()
        if (latest) {
          setAudit(latest)
          setAuditState('ready')
        } else if (!auditedOnce.current) {
          auditedOnce.current = true
          await runAudit()
        }
      } catch (e) {
        handleFailure(e)
        // No snapshot endpoint result — try running a fresh audit once.
        if (!auditedOnce.current && !isGmailReconnect(e)) {
          auditedOnce.current = true
          await runAudit()
        } else {
          setAuditState('error')
        }
      }
    })()
  }, [refreshTaxonomy, refreshRuns, runAudit, handleFailure])

  // The live feed for the active run.
  const feed = useRunFeed(activeRunId)
  useEffect(() => {
    if (!feed.terminalType) return
    // Run ended — collapse the feed to its run card and refresh counts.
    setActiveRunId(null)
    void refreshRuns()
    void runAudit()
  }, [feed.terminalType, refreshRuns, runAudit])

  const startRun = async () => {
    setStarting(true)
    setStartError(null)
    try {
      const { run_id } = await api.runs.start()
      setActiveRunId(run_id)
      void refreshRuns()
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        // A run is already active — attach to it instead of erroring.
        await refreshRuns()
      } else {
        setStartError(handleFailure(e))
      }
    } finally {
      setStarting(false)
    }
  }

  const latest = runs[0] ?? null
  const runActive = activeRunId != null
  const resume = !runActive && latest?.status === 'interrupted'
  const disabledReason = needsReconnect
    ? 'Reconnect Gmail first — the agent has no access right now.'
    : me.gmail_status === 'none' && !needsReconnect
      ? 'Gmail is not connected.'
      : null

  const activeRun = activeRunId ? (runs.find((r) => r.id === activeRunId) ?? null) : null

  return (
    <main className="mx-auto max-w-[var(--zi-content-max)] space-y-6 px-6 py-8">
      <Header me={me} needsReconnect={needsReconnect} onSignedOut={onSignedOut} />

      <CommandStrip
        audit={audit}
        auditState={auditState}
        onRetryAudit={() => void runAudit()}
        runActive={runActive}
        disabledReason={disabledReason}
        resume={resume}
        onClean={() => void startRun()}
        starting={starting}
      />

      {startError && <ErrorNote message={startError} onRetry={() => void startRun()} />}

      {runActive && (
        <ActivityFeed feed={feed} chunkLimit={activeRun?.chunk_limit ?? 50} />
      )}

      {runsError ? (
        <ErrorNote message={runsError} onRetry={() => void refreshRuns()} />
      ) : (
        <RunTimeline
          runs={runs.filter((r) => r.id !== activeRunId)}
          onChanged={() => void refreshRuns()}
        />
      )}

      <TaxonomyPanel
        categories={categories}
        loading={taxonomyLoading}
        loadError={taxonomyError}
        onChanged={() => void refreshTaxonomy()}
      />

      <LedgerStub />
      <CostsStub />
      <ProfilesStub />
    </main>
  )
}
