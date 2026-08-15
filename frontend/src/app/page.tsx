'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import { api, CONNECT_URL, isUnauthenticated } from '@/lib/api'
import { isRunActive, type Cluster, type Me, type Run } from '@/lib/types'
import { sseLive } from '@/lib/sseLive'
import { DryRunBanner, LeftRail, StatusPill } from '@/components/Chrome'
import { AccountMenu } from '@/components/AccountMenu'
import { Homepage } from '@/components/home/Homepage'
import { NoScriptFrontDoor } from '@/components/home/NoScriptFrontDoor'
import { Onboarding } from '@/components/Onboarding'
import { BTN } from '@/lib/tokens'
import { ClusterCard } from '@/components/ClusterCard'
import SettingsPanel from '@/components/Settings'
import { RunProgress } from '@/components/RunProgress'
import { InboxSummary } from '@/components/InboxSummary'
import { EmptyState, ErrorState, SkeletonRows } from '@/components/States'
import { RunSummaryCard } from '@/components/RunSummary'
import { ResumeBanner } from '@/components/ResumeBanner'
import { InboxZeroCard } from '@/components/InboxZeroCard'
import { LiveRunFeed } from '@/components/LiveRunFeed'
import { ActivityDrawer, openActivityDrawer } from '@/components/ActivityDrawer'

/**
 * In flight = not terminal AND not `resumable`.
 *
 * `resumable` is non-terminal (one click puts it back to `running`) but nothing is
 * executing, so it must not poll, must not show a progress bar and must not disable
 * the Triage button — it shows the Resume banner (ui.md screen 13) instead.
 */
const RUN_ACTIVE = (status: string) => isRunActive(status) && status !== 'resumable'

/** Set once the user has seen the first-run flow through to its completion
 *  line. Onboarding is never shown again for this user on this device. */
const ONBOARDING_DONE_KEY = 'zi_onboarding_done'

export default function Dashboard() {
  const [me, setMe] = useState<Me | null>(null)
  const [meLoading, setMeLoading] = useState(true)
  const [meError, setMeError] = useState<unknown>(null)

  /**
   * The front-door gate (spec/ui.md screens 19 & 25).
   *
   * `null` = we have not yet asked. `true` = /api/me said `unauthenticated`, so
   * the visitor gets the HOMEPAGE and nothing else — not a console skeleton,
   * not a flash of console chrome, not a spinner that resolves to nothing.
   */
  const [signedOut, setSignedOut] = useState<boolean | null>(null)
  /** True only when a session existed and then went away mid-use. */
  const [wasSignedIn, setWasSignedIn] = useState(false)
  const [onboardingDone, setOnboardingDone] = useState(false)

  /**
   * Has `GET /api/runs/latest` answered yet, and was this user's history empty
   * when it did?
   *
   * This is deliberately latched on the FIRST answer. "No completed run yet" is
   * not a safe onboarding test on its own: a returning user whose latest run is
   * still `running` also has no completed run, and dropping them into a
   * first-run flow mid-run would be a serious regression. Only a user with *no
   * run at all* at first paint is new, and they stay in onboarding for the whole
   * of that first run.
   */
  const [runLoaded, setRunLoaded] = useState(false)
  const [newUser, setNewUser] = useState<boolean | null>(null)

  useEffect(() => {
    try {
      setOnboardingDone(window.localStorage.getItem(ONBOARDING_DONE_KEY) === '1')
    } catch {
      // Storage unavailable (private mode) — a completed run still ends onboarding.
    }
  }, [])

  const finishOnboarding = useCallback(() => {
    try {
      window.localStorage.setItem(ONBOARDING_DONE_KEY, '1')
    } catch {
      // Non-fatal: the completed run keeps the user out of onboarding anyway.
    }
    setOnboardingDone(true)
  }, [])

  /** Any /api/* returning `unauthenticated` mid-session sends the user back to
   *  the homepage with one plain sentence — never a wall of failed panels. */
  const handleAuthLoss = useCallback((e: unknown): boolean => {
    if (!isUnauthenticated(e)) return false
    setSignedOut(true)
    return true
  }, [])

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
      setSignedOut(false)
      setWasSignedIn(true)
    } catch (e) {
      if (isUnauthenticated(e)) {
        // The normal signed-out path, not an error. Render the front door.
        setSignedOut(true)
      } else {
        setMeError(e)
      }
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
      if (!handleAuthLoss(e)) setClustersError(e)
    } finally {
      setClustersLoading(false)
    }
  }, [handleAuthLoss])

  // Resume the last completed/running run on load
  useEffect(() => {
    if (!connection || run) return
    let cancelled = false
    void api.latestRun().then(latest => {
      if (cancelled) return
      setNewUser(prev => (prev === null ? latest == null : prev))
      setRunLoaded(true)
      if (!latest) return
      setRun(latest)
      if (!RUN_ACTIVE(latest.status)) void loadClusters(latest.id)
    }).catch(() => {
      // No prior run yet — empty state is the correct fallback
      if (cancelled) return
      setNewUser(prev => (prev === null ? true : prev))
      setRunLoaded(true)
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
        if (!handleAuthLoss(e)) setRunError(e)
      }
    }, 1000)
    return () => {
      if (pollRef.current) clearInterval(pollRef.current)
      pollRef.current = null
    }
  }, [run, loadClusters, handleAuthLoss])

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

  /** After POST /api/runs/{id}/resume: re-read the row so the progress bar picks
   *  up at items_decided, not 0, and the 1s poll restarts. */
  const resumeRun = useCallback(async () => {
    if (!run) return
    try {
      setRun(await api.run(run.id))
      setRefreshKey(k => k + 1)
    } catch (e) {
      setRunError(e)
    }
  }, [run])

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

  // ── The front door gate (spec/ui.md screens 19, 21, 25) ──────────────────
  //
  // Ordering here is the whole point: a signed-out visitor must NEVER see the
  // console, not even skeletally, so the homepage branch is evaluated before a
  // single console element is constructed.

  if (signedOut) {
    return <Homepage signedOutNotice={wasSignedIn} />
  }

  // Before /api/me answers we know nothing — so we render neither surface.
  // A neutral wordmark is not a console and never resolves to nothing: the
  // very next render is either the homepage or the console.
  // A connected user whose run history has not answered yet is not yet
  // classifiable as new-or-returning. Rendering the console here would flash it
  // at a brand-new user; rendering onboarding would flash it at a returning one.
  // So we render neither, for the one request it takes to know.
  const undecided = Boolean(me && connection && !runLoaded)

  if (signedOut === null || (meLoading && !me) || undecided) {
    return (
      <div className="flex min-h-screen flex-col items-center justify-center bg-zi-bg">
        <p role="status" className="zi-body text-zi-fg-muted">
          Zero Inbox — checking your session…
        </p>
        <NoScriptFrontDoor />
      </div>
    )
  }

  // Signed in with no mailbox, or a first run not yet seen through: onboarding,
  // never the cluster list with an empty state.
  const hasCompletedRun = run ? run.status === 'completed' : false
  const inFirstRun = newUser === true && !hasCompletedRun && !onboardingDone
  if (me && (!connection || inFirstRun)) {
    return (
      <>
      <Onboarding
        connection={connection ?? null}
        run={run}
        settings={me.settings}
        onSettingsChange={s => setMe({ ...me, settings: s })}
        onFinish={finishOnboarding}
        onSignOutHint={
          <AccountMenu
            email={me.user.email}
            displayName={me.user.display_name}
            onAccountSecurity={() => {
              setActiveView('settings')
              finishOnboarding()
            }}
            onSettings={() => {
              setActiveView('settings')
              finishOnboarding()
            }}
          />
        }
      />
      {/* Mounted on the signed-in branch only — see layout.tsx. */}
      <ActivityDrawer />
      </>
    )
  }

  return (
    <div className="min-h-screen bg-zi-bg text-zi-fg">
      <DryRunBanner dryRun={dryRun} />

      <header
        role="banner"
        className="flex flex-wrap items-center justify-between gap-3 border-b border-zi-border bg-zi-bg px-4 py-2.5"
      >
        <div className="flex items-center gap-3">
          <h1 className="zi-h2">Zero Inbox</h1>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <StatusPill tone={statusTone}>
            {run ? `Run ${run.status}` : 'No run yet'}
          </StatusPill>
          {connection ? (
            <button
              type="button"
              data-testid="run-triage"
              onClick={() => void startTriage(false)}
              disabled={starting || (run ? RUN_ACTIVE(run.status) : false)}
              title={
                run && RUN_ACTIVE(run.status)
                  ? 'A run is already in progress — wait for it to finish or cancel it first.'
                  : 'Classifies your inbox and archives what it is confident about.'
              }
              className={`${BTN.primary} disabled:cursor-not-allowed`}
            >
              {starting ? <span className="zi-spinner" aria-hidden="true" /> : null}
              {starting ? 'Starting triage…' : 'Triage inbox'}
            </button>
          ) : (
            <a href={CONNECT_URL} className={BTN.primary}>
              Connect Gmail
            </a>
          )}

          {/* Screen 23 — the account menu replaces the bare address. */}
          {me ? (
            <AccountMenu
              email={connection?.account_email ?? me.user.email}
              displayName={me.user.display_name}
              onAccountSecurity={() => setActiveView('settings')}
              onSettings={() => setActiveView('settings')}
            />
          ) : null}
        </div>
      </header>

      {/* < 768px: the rail becomes a horizontal tab strip above the content.
          The Inbox-Zero card and the live feed are never what gets collapsed. */}
      <div className="flex flex-col items-stretch md:flex-row">
        <LeftRail
          onNavigate={label => setActiveView(label === 'Settings' ? 'settings' : 'triage')}
          active={activeView === 'settings' ? 'Settings' : 'Triage'}
        />

        <main
          id={activeView === 'settings' ? 'settings-panel' : 'triage-history'}
          className="mx-auto w-full min-w-0 max-w-[1120px] flex-1 space-y-4 p-4"
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
          ) : (
            <>
              <InboxSummary refreshKey={refreshKey} />

              {/* Phase 7 — how far from zero, and why (ui.md screen 16) */}
              <InboxZeroCard
                runId={run?.id ?? null}
                autoActThreshold={me?.settings.auto_act_threshold ?? null}
                confidenceFloor={me?.settings.confidence_floor ?? null}
              />

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

              {run && run.status === 'resumable' ? (
                <ResumeBanner
                  run={{
                    id: run.id,
                    items_total: run.items_total,
                    items_decided: run.items_decided,
                  }}
                  onResumed={() => void resumeRun()}
                  onStartFresh={() => void startTriage(false)}
                />
              ) : null}

              {run && run.status !== 'resumable' ? (
                <RunProgress
                  run={run}
                  onCancel={() => void cancelRun()}
                  cancelling={cancelling}
                  fetchedSoFar={fetchedSoFar}
                />
              ) : null}

              {/* Phase 7 — THE live run feed (ui.md screen 18).
                  Inline, under the progress bar, above the cluster list, with
                  no click and no toggle. Phase 6 shipped this same feed into a
                  drawer that is closed by default, so the user never saw the
                  run happen. Delivered is not shipped; visible is shipped. */}
              {run ? (
              <LiveRunFeed
                active={RUN_ACTIVE(run.status)}
                threadCount={run.items_total || fetchedSoFar}
                onSeeAllActivity={openActivityDrawer}
                idleSummary={
                  run.finished_at
                    ? `Last run: ${(run.counts?.applied ?? run.items_decided ?? 0).toLocaleString()} decisions · ${run.status}`
                    : null
                }
              />
              ) : null}

              <section aria-label="Triage history" className="space-y-2">
                <div className="flex flex-wrap items-baseline justify-between gap-2">
                  <h2 className="zi-h3 tracking-wide text-zi-fg-muted uppercase">
                    Triage History
                  </h2>
                  {clusters ? (
                    <p className="zi-caption text-zi-fg-muted">
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
                        className={`${BTN.primary} disabled:cursor-not-allowed`}
                        title="Classifies your inbox and archives what it is confident about."
                      >
                        {starting ? 'Starting triage…' : 'Triage inbox'}
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

      <footer role="contentinfo" className="border-t border-zi-border bg-zi-bg px-4 py-3 zi-caption text-zi-fg-muted">
        {dryRun
          ? 'Dry run is on. Triage decisions are recorded but Gmail is not touched. Turn dry-run off in Settings to act for real.'
          : 'Dry run is off. Triage actions are applied autonomously to your Gmail mailbox.'}
      </footer>

      {/* Mounted on the signed-in branch only — see layout.tsx. */}
      <ActivityDrawer />
    </div>
  )
}
