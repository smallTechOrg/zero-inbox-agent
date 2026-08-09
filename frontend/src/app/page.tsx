'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import { api, AUTH_START_URL } from '@/lib/api'
import { isRunActive, type Cluster, type Me, type Run } from '@/lib/types'
import { DryRunBanner, LeftRail, StatusPill } from '@/components/Chrome'
import { ConnectCard } from '@/components/ConnectCard'
import { ClusterCard } from '@/components/ClusterCard'
import { NeedsYourCall } from '@/components/NeedsYourCall'
import { RunProgress } from '@/components/RunProgress'
import { EmptyState, ErrorState, SkeletonRows } from '@/components/States'
import { StubButton, StubPanel } from '@/components/Stub'

/** Anything that is not a terminal status (completed / failed / cancelled). */
const RUN_ACTIVE = isRunActive

export default function Dashboard() {
  const [me, setMe] = useState<Me | null>(null)
  const [meLoading, setMeLoading] = useState(true)
  const [meError, setMeError] = useState<unknown>(null)

  const [run, setRun] = useState<Run | null>(null)
  const [runError, setRunError] = useState<unknown>(null)
  const [starting, setStarting] = useState(false)
  const [cancelling, setCancelling] = useState(false)

  const [clusters, setClusters] = useState<Cluster[] | null>(null)
  const [clustersLoading, setClustersLoading] = useState(false)
  const [clustersError, setClustersError] = useState<unknown>(null)
  const [refreshKey, setRefreshKey] = useState(0)

  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const loadMe = useCallback(async () => {
    setMeLoading(true)
    setMeError(null)
    try {
      setMe(await api.me())
    } catch (e) {
      setMeError(e)
    } finally {
      setMeLoading(false)
    }
  }, [])

  useEffect(() => {
    void loadMe()
  }, [loadMe])

  const connection = me?.connections?.find(c => c.status !== 'revoked') ?? me?.connections?.[0]

  const loadClusters = useCallback(async (runId: string) => {
    setClustersLoading(true)
    setClustersError(null)
    try {
      setClusters(await api.clusters(runId))
    } catch (e) {
      setClustersError(e)
    } finally {
      setClustersLoading(false)
    }
  }, [])

  // Poll run progress every 1s while active; results stream in as they land.
  useEffect(() => {
    if (!run || !RUN_ACTIVE(run.status)) return
    const runId = run.id
    pollRef.current = setInterval(async () => {
      try {
        const fresh = await api.run(runId)
        setRun(fresh)
        setRunError(null)
        void loadClusters(runId)
        setRefreshKey(k => k + 1)
        if (!RUN_ACTIVE(fresh.status) && pollRef.current) {
          clearInterval(pollRef.current)
          pollRef.current = null
        }
      } catch (e) {
        setRunError(e)
      }
    }, 1000)
    return () => {
      if (pollRef.current) clearInterval(pollRef.current)
      pollRef.current = null
    }
  }, [run, loadClusters])

  const startTriage = useCallback(async () => {
    if (!connection) return
    setStarting(true)
    setRunError(null)
    try {
      const { run_id } = await api.startTriage(connection.id, 200)
      const fresh = await api.run(run_id)
      setRun(fresh)
      setClusters(null)
      void loadClusters(run_id)
      setRefreshKey(k => k + 1)
    } catch (e) {
      setRunError(e)
    } finally {
      setStarting(false)
    }
  }, [connection, loadClusters])

  const cancelRun = useCallback(async () => {
    if (!run) return
    setCancelling(true)
    try {
      await api.cancelRun(run.id)
      setRun(await api.run(run.id))
    } catch (e) {
      setRunError(e)
    } finally {
      setCancelling(false)
    }
  }, [run])

  const statusTone = !run
    ? 'idle'
    : RUN_ACTIVE(run.status)
      ? 'running'
      : run.status === 'failed'
        ? 'bad'
        : 'ok'

  return (
    <div className="min-h-screen">
      <DryRunBanner />

      <header className="flex flex-wrap items-center justify-between gap-3 border-b border-gray-200 bg-white px-4 py-2.5">
        <div className="flex items-center gap-3">
          <h1 className="text-base font-bold tracking-tight text-gray-900">Zero Inbox Agent</h1>
          {meLoading ? (
            <span className="text-xs text-gray-500">Loading account…</span>
          ) : connection ? (
            <span
              data-testid="connected-address"
              className="rounded bg-gray-100 px-2 py-0.5 font-mono text-xs text-gray-700"
            >
              {connection.account_email}
            </span>
          ) : (
            <span className="text-xs text-gray-500">No mailbox connected</span>
          )}
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <StatusPill tone={statusTone}>
            {run ? `Run ${run.status}` : 'No run yet'}
          </StatusPill>
          <StubButton label="Model: auto" phase={3} />
          {connection ? (
            <button
              type="button"
              data-testid="run-triage"
              onClick={() => void startTriage()}
              disabled={starting || (run ? RUN_ACTIVE(run.status) : false)}
              className="rounded-lg bg-gray-900 px-3.5 py-1.5 text-sm font-semibold text-white hover:bg-gray-700 focus:ring-2 focus:ring-gray-400 focus:outline-none disabled:opacity-50"
            >
              {starting ? 'Starting…' : 'Run triage (200 threads)'}
            </button>
          ) : (
            <a
              href={AUTH_START_URL}
              className="rounded-lg bg-gray-900 px-3.5 py-1.5 text-sm font-semibold text-white hover:bg-gray-700 focus:ring-2 focus:ring-gray-400 focus:outline-none"
            >
              Connect Gmail
            </a>
          )}
        </div>
      </header>

      <div className="flex items-stretch">
        <LeftRail />

        <main id="triage-queue" className="min-w-0 flex-1 space-y-4 p-4">
          {meLoading ? (
            <SkeletonRows rows={3} label="Loading your account…" />
          ) : meError ? (
            <ErrorState error={meError} onRetry={() => void loadMe()} />
          ) : !connection ? (
            <ConnectCard />
          ) : (
            <>
              {runError ? <ErrorState error={runError} onRetry={() => void startTriage()} /> : null}

              {run ? (
                <RunProgress run={run} onCancel={() => void cancelRun()} cancelling={cancelling} />
              ) : null}

              {run ? <NeedsYourCall runId={run.id} refreshKey={refreshKey} /> : null}

              <section aria-label="Triage queue" className="space-y-2">
                <div className="flex items-baseline justify-between gap-2">
                  <h2 className="text-sm font-bold tracking-wide text-gray-700 uppercase">
                    Triage queue
                  </h2>
                  {clusters ? (
                    <p className="text-xs text-gray-500">
                      {clusters.length} clusters ·{' '}
                      {clusters.reduce((n, c) => n + c.item_count, 0)} threads
                    </p>
                  ) : null}
                </div>

                {!run ? (
                  <EmptyState
                    title="No triage run yet"
                    body="Run triage over your 200 most recent inbox threads. Phase 1 is dry run — the agent decides and explains, but never touches your mailbox."
                    action={
                      <button
                        type="button"
                        onClick={() => void startTriage()}
                        disabled={starting}
                        className="rounded-lg bg-gray-900 px-4 py-2 text-sm font-semibold text-white hover:bg-gray-700 focus:ring-2 focus:ring-gray-400 focus:outline-none disabled:opacity-50"
                      >
                        {starting ? 'Starting…' : 'Run triage (200 threads)'}
                      </button>
                    }
                  />
                ) : clustersError ? (
                  <ErrorState error={clustersError} onRetry={() => void loadClusters(run.id)} />
                ) : clusters === null && clustersLoading ? (
                  <SkeletonRows rows={5} label="Loading clusters…" />
                ) : clusters && clusters.length > 0 ? (
                  <ul className="space-y-2">
                    {clusters.map(c => (
                      <ClusterCard key={c.id} cluster={c} />
                    ))}
                  </ul>
                ) : RUN_ACTIVE(run.status) ? (
                  <SkeletonRows rows={5} label="Triaging — clusters appear as threads are decided…" />
                ) : (
                  <EmptyState
                    title="No clusters in this run"
                    body="The run finished without producing any clusters. If that looks wrong, start another run."
                  />
                )}
              </section>
            </>
          )}
        </main>

        <aside
          aria-label="Coming soon"
          className="hidden w-72 shrink-0 space-y-3 border-l border-gray-200 bg-gray-50 p-3 xl:block"
        >
          <p className="text-[11px] font-semibold tracking-wide text-gray-500 uppercase">
            Not built yet — later phases
          </p>
          <StubPanel
            title="Cost panel"
            phase={3}
            description="Run spend, month-to-date total and the rules-vs-LLM handling ratio."
          />
          <StubPanel
            title="Model dropdown"
            phase={3}
            description="Pick which NVIDIA free model triages your mail."
          />
          <StubPanel
            title="VIP / never-hide list"
            phase={2}
            description="People whose mail is never hidden, whatever the agent thinks."
          />
          <StubPanel
            title="Priorities profile"
            phase={2}
            description="A plain-English description of what matters to you, fed into triage."
          />
          <StubPanel
            title="Backlog cleanup"
            phase={3}
            description="Clean historical mail in dated, cancellable, resumable chunks."
          />
          <StubPanel
            title="Daily digest"
            phase={3}
            description="A daily summary of everything that was hidden, so nothing vanishes silently."
          />
          <StubPanel
            title="Unsubscribe suggestions"
            phase={3}
            description="Senders you never open, with a one-click unsubscribe suggestion."
          />
          <StubPanel
            title="Stale threads"
            phase={3}
            description="Threads awaiting a reply from you, or from someone else, for too long."
          />
        </aside>
      </div>

      <footer className="border-t border-gray-200 bg-white px-4 py-3 text-xs text-gray-500">
        Phase 1 · dry run only. Approving or rejecting records your intent in the database; no Gmail
        message is archived, labelled, deleted or moved.
      </footer>
    </div>
  )
}
