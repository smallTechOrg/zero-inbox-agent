'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import { api, AUTH_START_URL } from '@/lib/api'
import { isRunActive, type Cluster, type Me, type Run } from '@/lib/types'
import { DryRunBanner, LeftRail, StatusPill } from '@/components/Chrome'
import { ConnectCard } from '@/components/ConnectCard'
import { ClusterCard, type ClusterCardHandle } from '@/components/ClusterCard'
import SettingsPanel from '@/components/Settings'
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
  const [activeView, setActiveView] = useState<'triage' | 'settings'>('triage')

  const [clusters, setClusters] = useState<Cluster[] | null>(null)
  const [clustersLoading, setClustersLoading] = useState(false)
  const [clustersError, setClustersError] = useState<unknown>(null)
  const clusterRefs = useRef<Map<string, ClusterCardHandle | null>>(new Map())
  const focusedId = useRef<string | null>(null)
  const [refreshKey, setRefreshKey] = useState(0)

  // Keyboard sweep: j/k move between clusters, a approve, x reject,
  // A approve whole cluster, Enter expand/collapse. (spec/ui.md §2)
  const handleKeyDown = useCallback(
    async (e: React.KeyboardEvent<HTMLUListElement>) => {
      if (!clusters || clusters.length === 0) return
      const ids = clusters.map(c => c.id)
      if (focusedId.current === null || !ids.includes(focusedId.current)) {
        focusedId.current = ids[0]
      }
      const idx = ids.indexOf(focusedId.current)
      const card = clusterRefs.current.get(focusedId.current)
      if (e.key === 'j') {
        e.preventDefault()
        focusedId.current = ids[(idx + 1) % ids.length]
      } else if (e.key === 'k') {
        e.preventDefault()
        focusedId.current = ids[(idx - 1 + ids.length) % ids.length]
      } else if (e.key === 'a') {
        e.preventDefault()
        void card?.approve()
      } else if (e.key === 'x') {
        e.preventDefault()
        void card?.reject()
      } else if (e.key === 'A') {
        e.preventDefault()
        void card?.approveAll()
      } else if (e.key === 'Enter') {
        e.preventDefault()
        card?.toggle()
      }
    },
    [clusters],
  )

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

  // Resume the last completed/running run on load — a triage pass already paid
  // for should be visible on refresh, not thrown away and re-run from scratch.
  useEffect(() => {
    if (!connection || run) return
    let cancelled = false
    void api.latestRun().then(latest => {
      if (cancelled || !latest) return
      setRun(latest)
      // A resumed *completed* run never enters the active-run polling effect
      // below (it only fires for RUN_ACTIVE statuses), so its clusters must be
      // fetched here explicitly or the queue stays empty after a refresh.
      if (!RUN_ACTIVE(latest.status)) void loadClusters(latest.id)
    }).catch(() => {
      // No prior run yet, or it couldn't be loaded — the empty state below
      // (Start Triage) is the correct fallback, so this is silently ignored.
    })
    return () => {
      cancelled = true
    }
  }, [connection, run, loadClusters])

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

  const startTriage = useCallback(
    async (onlyNew = false) => {
      if (!connection) return
      setStarting(true)
      setRunError(null)
      try {
        const { run_id } = await api.startTriage(connection.id, 200, onlyNew)
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

  const [approvingAll, setApprovingAll] = useState(false)
  const [approveAllResult, setApproveAllResult] = useState<string | null>(null)

  const approveAllClusters = useCallback(async () => {
    if (!run) return
    setApprovingAll(true)
    setApproveAllResult(null)
    setRunError(null)
    try {
      const res = await api.reviewAllClusters(run.id, 'approved')
      let message =
        res.updated === 0
          ? 'Nothing left to approve — everything visible is already decided.'
          : dryRun
            ? `${res.updated} thread(s) approved — recorded only, Gmail untouched (dry run is on).`
            : `${res.updated} thread(s) approved — archiving in Gmail now…`
      if (res.skipped_needs_your_call > 0) {
        message += ` ${res.skipped_needs_your_call} thread(s) below the confidence floor were left for you to decide individually.`
      }
      if (!dryRun && res.updated > 0) {
        // Same pattern ClusterCard uses: re-read what actually ended up
        // approved, then apply — needs_your_call items were already excluded
        // server-side, so nothing here can archive something below the floor.
        const approvedItems = await api.itemsByStatus(run.id, 'approved')
        const ids = approvedItems.map(i => i.decision_id)
        if (ids.length > 0) {
          const results = await api.applyDecisions(ids)
          const archived = results.filter(r => r.action_log_id).length
          message = `${archived} thread(s) approved and archived in Gmail.`
        }
      }
      setApproveAllResult(message)
      void loadClusters(run.id)
      setRefreshKey(k => k + 1)
    } catch (e) {
      setRunError(e)
    } finally {
      setApprovingAll(false)
    }
  }, [run, dryRun, loadClusters])

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
          <StubButton label="Model: auto" phase={3} />
          {connection ? (
            <>
              <button
                type="button"
                data-testid="run-triage"
                onClick={() => void startTriage(false)}
                disabled={starting || (run ? RUN_ACTIVE(run.status) : false)}
                className="rounded-lg bg-gray-900 px-3.5 py-1.5 text-sm font-semibold text-white hover:bg-gray-700 focus:ring-2 focus:ring-gray-400 focus:outline-none disabled:opacity-50"
              >
                {starting ? 'Starting…' : 'Run triage (200 threads)'}
              </button>
              {run ? (
                <button
                  type="button"
                  data-testid="run-triage-new-only"
                  title="Only fetch and classify threads newer than your last completed run — skips re-listing mail you've already seen."
                  onClick={() => void startTriage(true)}
                  disabled={starting || (run ? RUN_ACTIVE(run.status) : false)}
                  className="rounded-lg border border-gray-300 bg-white px-3.5 py-1.5 text-sm font-semibold text-gray-800 hover:bg-gray-50 focus:ring-2 focus:ring-gray-400 focus:outline-none disabled:opacity-50"
                >
                  {starting ? 'Starting…' : 'Fetch new mail only'}
                </button>
              ) : null}
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
          id={activeView === 'settings' ? 'settings-panel' : 'triage-queue'}
          className="min-w-0 flex-1 space-y-4 p-4"
        >
          {activeView === 'settings' ? (
            <SettingsPanel
              settings={me?.settings ?? null}
              onSettingsChange={s => {
                /* SettingsPanel already persisted this via PATCH /api/settings — mirror it locally. */
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
              {runError ? <ErrorState error={runError} onRetry={() => void startTriage()} /> : null}

              {run ? (
                <RunProgress run={run} onCancel={() => void cancelRun()} cancelling={cancelling} />
              ) : null}

              {run ? (
                <NeedsYourCall runId={run.id} refreshKey={refreshKey} dryRun={dryRun} />
              ) : null}

              <section aria-label="Triage queue" className="space-y-2">
                <div className="flex flex-wrap items-baseline justify-between gap-2">
                  <h2 className="text-sm font-bold tracking-wide text-gray-700 uppercase">
                    Triage queue
                  </h2>
                  <div className="flex items-center gap-3">
                    {clusters ? (
                      <p className="text-xs text-gray-500">
                        {clusters.length} clusters ·{' '}
                        {clusters.reduce((n, c) => n + c.item_count, 0)} threads
                      </p>
                    ) : null}
                    {clusters && clusters.length > 0 && run ? (
                      <button
                        type="button"
                        data-testid="approve-all-clusters"
                        title="Approve every proposed cluster in this run. Threads below the confidence floor stay in Needs your call for you to decide individually."
                        onClick={() => void approveAllClusters()}
                        disabled={approvingAll || RUN_ACTIVE(run.status)}
                        className="rounded-lg bg-emerald-700 px-3 py-1 text-xs font-semibold text-white hover:bg-emerald-800 focus:ring-2 focus:ring-emerald-400 focus:outline-none disabled:opacity-50"
                      >
                        {approvingAll ? 'Approving…' : 'Approve all'}
                      </button>
                    ) : null}
                  </div>
                </div>
                {approveAllResult ? (
                  <p
                    data-testid="approve-all-result"
                    className="rounded-md bg-emerald-50 px-3 py-2 text-xs text-emerald-800"
                  >
                    {approveAllResult}
                  </p>
                ) : null}

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
                  <ul className="space-y-2" onKeyDown={handleKeyDown} tabIndex={-1}>
                    {clusters.map(c => (
                      <ClusterCard
                        key={c.id}
                        cluster={c}
                        dryRun={dryRun}
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
          <p className="rounded-lg border border-emerald-300 bg-emerald-50 p-2.5 text-[11px] text-emerald-900">
            VIP list and priorities profile are real now — open <strong>Settings</strong> to edit
            them.
          </p>
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
        {dryRun
          ? 'Dry run is on. Approving or rejecting records your intent in the database; no Gmail message is archived, labelled, deleted or moved. Turn dry-run off in Settings to act for real.'
          : 'Dry run is off. Approving a decision really archives and labels the matching thread in Gmail; rejecting still only records intent and never mutates your mailbox.'}
      </footer>
    </div>
  )
}
