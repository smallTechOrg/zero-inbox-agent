'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import { api, AUTH_START_URL } from '@/lib/api'
import { isRunActive, type Cluster, type Me, type Run } from '@/lib/types'
import { sseLive } from '@/lib/sseLive'
import { DryRunBanner, LeftRail, StatusPill } from '@/components/Chrome'
import { ConnectCard } from '@/components/ConnectCard'
import { ClusterCard } from '@/components/ClusterCard'
import SettingsPanel from '@/components/Settings'
import { RunProgress } from '@/components/RunProgress'
import { InboxSummary } from '@/components/InboxSummary'
import { EmptyState, ErrorState, SkeletonRows } from '@/components/States'
import { RunSummaryCard } from '@/components/RunSummary'

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
  const [activeView, setActiveView] = useState<'triage' | 'settings'>('triage')

  const [clusters, setClusters] = useState<Cluster[] | null>(null)
  const [clustersLoading, setClustersLoading] = useState(false)
  const [clustersError, setClustersError] = useState<unknown>(null)
  const clusterRefs = useRef<Map<string, unknown>>(new Map())
  const [refreshKey, setRefreshKey] = useState(0)

  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  // fetchedSoFar: updated by fetch_progress SSE events during the inbox-reading phase
  const [fetchedSoFar, setFetchedSoFar] = useState(0)

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
  const dryRun = me?.settings.dry_run ?? true

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

  // Resume the last completed/running run on load
  useEffect(() => {
    if (!connection || run) return
    let cancelled = false
    void api.latestRun().then(latest => {
      if (cancelled || !latest) return
      setRun(latest)
      if (!RUN_ACTIVE(latest.status)) void loadClusters(latest.id)
    }).catch(() => {
      // No prior run yet — empty state is the correct fallback
    })
    return () => {
      cancelled = true
    }
  }, [connection, run, loadClusters])

  // Poll run progress every 1s while active
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

  // Drive fetchedSoFar from the ActivityDrawer's shared SSE connection (sseLive)
  // instead of opening a second EventSource.
  useEffect(() => {
    return sseLive.subscribeFetchedSoFar(setFetchedSoFar)
  }, [])

  const startTriage = useCallback(
    async (onlyNew = false) => {
      if (!connection) return
      setStarting(true)
      setRunError(null)
      try {
        const { run_id } = await api.startTriage(connection.id, 10_000, onlyNew)
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
    },
    [connection, loadClusters],
  )

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

  // Suppress unused warning — ref used for cluster card handles (now read-only)
  void clusterRefs

  return (
    <div className="min-h-screen">
      <DryRunBanner dryRun={dryRun} />

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
          {connection ? (
            <>
              <button
                type="button"
                data-testid="run-triage"
                onClick={() => void startTriage(false)}
                disabled={starting || (run ? RUN_ACTIVE(run.status) : false)}
                className="rounded-lg bg-gray-900 px-3.5 py-1.5 text-sm font-semibold text-white hover:bg-gray-700 focus:ring-2 focus:ring-gray-400 focus:outline-none disabled:opacity-50"
              >
                {starting ? 'Starting…' : 'Triage inbox'}
              </button>
            </>
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
        <LeftRail
          onNavigate={label => setActiveView(label === 'Settings' ? 'settings' : 'triage')}
          active={activeView === 'settings' ? 'Settings' : 'Triage'}
        />

        <main
          id={activeView === 'settings' ? 'settings-panel' : 'triage-history'}
          className="min-w-0 flex-1 space-y-4 p-4"
        >
          {activeView === 'settings' ? (
            <SettingsPanel
              settings={me?.settings ?? null}
              onSettingsChange={s => {
                if (me) setMe({ ...me, settings: s })
              }}
            />
          ) : meLoading ? (
            <SkeletonRows rows={3} label="Loading your account…" />
          ) : meError ? (
            <ErrorState error={meError} onRetry={() => void loadMe()} />
          ) : !connection ? (
            <ConnectCard />
          ) : (
            <>
              <InboxSummary refreshKey={refreshKey} />

              {run && !RUN_ACTIVE(run.status) ? (
                <RunSummaryCard
                  runId={run.id}
                  onViewHistory={() => {
                    const el = document.getElementById('triage-history')
                    el?.scrollIntoView({ behavior: 'smooth' })
                  }}
                />
              ) : null}

              {runError ? <ErrorState error={runError} onRetry={() => void startTriage()} /> : null}

              {run ? (
                <RunProgress
                  run={run}
                  onCancel={() => void cancelRun()}
                  cancelling={cancelling}
                  fetchedSoFar={fetchedSoFar}
                />
              ) : null}

              <section aria-label="Triage history" className="space-y-2">
                <div className="flex flex-wrap items-baseline justify-between gap-2">
                  <h2 className="text-sm font-bold tracking-wide text-gray-700 uppercase">
                    Triage History
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
                    title="Connect your Gmail to start — triage runs automatically after connect"
                    body="The agent reads your inbox, applies your category rules, and archives what doesn't need your attention — automatically."
                    action={
                      <button
                        type="button"
                        onClick={() => void startTriage()}
                        disabled={starting}
                        className="rounded-lg bg-gray-900 px-4 py-2 text-sm font-semibold text-white hover:bg-gray-700 focus:ring-2 focus:ring-gray-400 focus:outline-none disabled:opacity-50"
                      >
                        {starting ? 'Starting…' : 'Triage inbox'}
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
                      <ClusterCard
                        key={c.id}
                        cluster={c}
                        readOnly={true}
                        ref={node => {
                          clusterRefs.current.set(c.id, node)
                        }}
                      />
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

      </div>

      <footer className="border-t border-gray-200 bg-white px-4 py-3 text-xs text-gray-500">
        {dryRun
          ? 'Dry run is on. Triage decisions are recorded but Gmail is not touched. Turn dry-run off in Settings to act for real.'
          : 'Dry run is off. Triage actions are applied autonomously to your Gmail mailbox.'}
      </footer>
    </div>
  )
}
