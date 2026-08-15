'use client'

/**
 * First run — spec/ui.md screen 21.
 *
 * Three steps, a persistent indicator that names the current step IN TEXT (never
 * a bare dot row), and a flow that ends in a deliberately designed moment of
 * trust: the first thread the agent KEEPS for a never-miss reason, shown before
 * it has archived anything. The product proves it protects before it proves it
 * cleans.
 *
 * Two things here are load-bearing and must not be softened:
 *  · the step-1 promise is the true one (see ConnectCard) — an onboarding flow
 *    that overstates safety is worse than no onboarding at all;
 *  · the step-3 keep callout is rendered ONLY when a never-miss keep genuinely
 *    occurs in the first 50 decisions. It is never fabricated, never seeded and
 *    never carried over from another run. If it does not happen, it is absent;
 *  · every statement about what will or did happen to the user's mail is derived
 *    from the REAL `dry_run` setting. Under dry run the pinned line says dry run
 *    is the reason nothing was archived — attributing it to the reviewer would
 *    be a plausible-sounding lie about the one thing the user is judging us on.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api } from '@/lib/api'
import { useSse } from '@/lib/SseContext'
import type { Connection, Run, Settings, ThreadClassifiedEvent } from '@/lib/types'
import { BTN, CONTENT, stateBarClass } from '@/lib/tokens'
import { ConnectCard } from '@/components/ConnectCard'
import { DryRunBanner } from '@/components/Chrome'
import { LiveRunFeed } from '@/components/LiveRunFeed'
import { openActivityDrawer } from '@/components/ActivityDrawer'

export type OnboardingStep = 1 | 2 | 3

const STEP_NAMES: Record<OnboardingStep, string> = {
  1: 'Connect your mailbox',
  2: 'What happens next',
  3: 'Watch it work',
}

/** The window inside which the moment-of-trust callout may appear (screen 21). */
export const KEEP_CALLOUT_WINDOW = 50

/**
 * Was this thread kept for a *never-miss* reason — reply history, VIP, or
 * time-sensitivity — as opposed to merely being an ordinary keep?
 *
 * Read off the real decision the agent published. If the reason is not one of
 * these, this returns null and no callout is shown.
 */
export function neverMissKeepReason(t: ThreadClassifiedEvent): string | null {
  if (t.action !== 'keep') return null
  const reasoning = (t.reasoning ?? '').toLowerCase()
  if (t.decided_by === 'sender_history' || /replied|reply history|corresponded/.test(reasoning)) {
    return 'you’ve replied to this sender before'
  }
  if (/\bvip\b/.test(reasoning)) return 'they’re on your VIP list'
  if (/time.sensitive|deadline|expires/.test(reasoning)) return 'it looks time-sensitive'
  return null
}

function StepIndicator({ step }: { step: OnboardingStep }) {
  return (
    <ol
      aria-label="First-run progress"
      data-testid="onboarding-steps"
      className="flex flex-wrap items-center gap-3"
    >
      {([1, 2, 3] as OnboardingStep[]).map(n => {
        const state = n < step ? 'done' : n === step ? 'current' : 'upcoming'
        return (
          <li
            key={n}
            aria-current={state === 'current' ? 'step' : undefined}
            className={`zi-caption rounded-zi-r-sm border px-2 py-1 ${
              state === 'current'
                ? 'border-zi-fg bg-zi-bg-inverse text-white'
                : state === 'done'
                  ? 'border-zi-ok bg-zi-ok-bg text-zi-ok'
                  : 'border-zi-border bg-zi-bg-subtle text-zi-fg-faint'
            }`}
          >
            <span className="zi-num">{n}</span> · {STEP_NAMES[n]}
            {state === 'done' ? ' — done' : state === 'current' ? ' — you are here' : ' — next'}
          </li>
        )
      })}
    </ol>
  )
}

export type OnboardingProps = {
  connection: Connection | null
  run: Run | null
  settings: Settings | null
  onSettingsChange: (s: Settings) => void
  /** Called when the user leaves onboarding for the steady-state console. */
  onFinish: () => void
  onSignOutHint?: React.ReactNode
}

export function Onboarding({
  connection,
  run,
  settings,
  onSettingsChange,
  onFinish,
  onSignOutHint,
}: OnboardingProps) {
  const { feed } = useSse()

  const [advanced, setAdvanced] = useState(false)
  const [dryRunSaving, setDryRunSaving] = useState(false)
  const [dryRunError, setDryRunError] = useState<string | null>(null)
  /** Armed only for the dangerous direction (turning dry run OFF). */
  const [confirmLive, setConfirmLive] = useState(false)
  const [undoing, setUndoing] = useState(false)
  const [undoResult, setUndoResult] = useState<string | null>(null)
  const [undoError, setUndoError] = useState<string | null>(null)

  const threadRows = useMemo(() => feed.filter(r => r.kind === 'thread'), [feed])
  const archivedRows = useMemo(() => feed.filter(r => r.kind === 'thread_archived'), [feed])
  const archivedCount = archivedRows.length || (run?.counts?.applied ?? 0)

  /**
   * The first never-miss keep, latched. `feed` is newest-first, so we scan the
   * oldest end of the first-50 window forwards. Once found it is pinned, so a
   * later reviewer flip does not make the moment of trust flicker away.
   */
  const latchedKeep = useRef<{ subject: string; reason: string } | null>(null)
  const window50 = threadRows.slice(-KEEP_CALLOUT_WINDOW)
  if (!latchedKeep.current) {
    for (let i = window50.length - 1; i >= 0; i--) {
      const row = window50[i]
      if (row.kind !== 'thread') continue
      const reason = neverMissKeepReason(row.data)
      if (reason && row.data.subject) {
        latchedKeep.current = { subject: row.data.subject, reason }
        break
      }
    }
  }
  const keepCallout = latchedKeep.current

  const step: OnboardingStep = !connection ? 1 : advanced || threadRows.length > 0 ? 3 : 2

  /**
   * `null` until `/api/me` has answered — and null is NOT treated as either
   * value. Nothing in this flow may claim "dry run is on" or "we will archive"
   * before the real setting is known.
   */
  const dryRun: boolean | null = settings ? settings.dry_run : null

  /**
   * The secondary control of screen 21, made honest.
   *
   * It used to hardcode `dry_run: true` and disable itself whenever dry run was
   * already on — which, given the server default of `true`, meant it shipped
   * permanently dead for every new user. It is now a real two-way control that
   * reflects the actual setting: it offers the direction the user is not
   * already in. Turning dry run OFF is the dangerous direction, so that one
   * arms a confirm step first; turning it ON is one click.
   */
  const setDryRun = useCallback(
    async (next: boolean) => {
      setDryRunSaving(true)
      setDryRunError(null)
      try {
        onSettingsChange(await api.updateSettings({ dry_run: next }))
        setConfirmLive(false)
      } catch (e) {
        setDryRunError(
          e instanceof Error
            ? e.message
            : next
              ? 'Could not switch to dry run.'
              : 'Could not turn dry run off.',
        )
      } finally {
        setDryRunSaving(false)
      }
    },
    [onSettingsChange],
  )

  const undoRun = useCallback(async () => {
    if (!run) return
    setUndoing(true)
    setUndoError(null)
    try {
      const res = await api.runs.undo(run.id)
      setUndoResult(`${res.reversed.toLocaleString()} threads restored to your inbox.`)
    } catch (e) {
      setUndoError(e instanceof Error ? e.message : 'Undo failed.')
    } finally {
      setUndoing(false)
    }
  }, [run])

  // Once rows start arriving we are past step 2 for good.
  useEffect(() => {
    if (threadRows.length > 0) setAdvanced(true)
  }, [threadRows.length])

  const runFinished = run ? ['completed', 'failed', 'cancelled'].includes(run.status) : false

  return (
    <div data-testid="onboarding" className="min-h-screen bg-zi-bg text-zi-fg">
      {/* The dry-run state is visible from the first screen of the first run —
          rendered only once the real setting is known, never assumed. */}
      {dryRun === null ? null : <DryRunBanner dryRun={dryRun} />}

      <header className="border-b border-zi-border bg-zi-bg">
        <div className={`${CONTENT} flex flex-wrap items-center justify-between gap-4 px-6 py-4`}>
          <span className="zi-h2">Zero Inbox</span>
          {onSignOutHint}
        </div>
      </header>

      <main id="main" className={`${CONTENT} space-y-6 px-6 py-8`}>
        <StepIndicator step={step} />

        {step === 1 ? (
          <>
            <h1 className="zi-h1">Step 1 — Connect your mailbox</h1>
            <ConnectCard />
          </>
        ) : null}

        {step === 2 ? (
          <section aria-labelledby="zi-step2-heading" className="zi-card rounded-zi-r-lg p-6">
            <h1 id="zi-step2-heading" className="zi-h1">
              Step 2 — What happens next
            </h1>
            <p className="zi-body mt-2 text-zi-fg-muted">
              Said before it happens, so nothing that follows is a surprise.
            </p>

            <ul className="mt-6 space-y-2" data-testid="what-happens-next">
              <li className="zi-body-lg">We&rsquo;re about to read your inbox headers.</li>
              <li className="zi-body-lg">We classify every thread in clusters.</li>
              <li className="zi-body-lg">We archive only what we&rsquo;re confident about.</li>
              <li className="zi-body-lg">
                We leave People, Urgent, Legal and anyone you&rsquo;ve replied to alone.
              </li>
              <li className="zi-body-lg">
                You&rsquo;ll watch every decision as it&rsquo;s made. Nothing is hidden from you and
                nothing is permanent.
              </li>
            </ul>

            <div className="mt-6 flex flex-wrap items-center gap-4">
              <button
                type="button"
                onClick={() => setAdvanced(true)}
                className={BTN.primary}
                data-testid="onboarding-watch"
              >
                Watch it work →
              </button>

              {dryRun === null ? (
                <span className="zi-caption text-zi-fg-muted" data-testid="dry-run-unknown">
                  Checking whether dry run is on…
                </span>
              ) : dryRun ? (
                /* Dry run is already on — offering to enable it again would be
                   meaningless, so the control offers the direction the user is
                   not in, behind a confirm step. */
                <button
                  type="button"
                  data-testid="start-in-dry-run"
                  onClick={() => (confirmLive ? void setDryRun(false) : setConfirmLive(true))}
                  disabled={dryRunSaving}
                  title={
                    confirmLive
                      ? 'Confirms turning dry run off. This pass will really archive mail — undoably, and never deleted.'
                      : 'Dry run is on, so this pass will archive nothing. Use this to turn it off and let the agent act for real.'
                  }
                  className={`${BTN.secondary} disabled:cursor-not-allowed`}
                >
                  {dryRunSaving ? <span className="zi-spinner" aria-hidden="true" /> : null}
                  {dryRunSaving
                    ? 'Turning dry run off…'
                    : confirmLive
                      ? 'Confirm — turn dry run off and archive for real'
                      : 'Dry run is on — run for real instead'}
                </button>
              ) : (
                <button
                  type="button"
                  data-testid="start-in-dry-run"
                  onClick={() => void setDryRun(true)}
                  disabled={dryRunSaving}
                  title="Turns on dry run, so this pass classifies everything and changes nothing."
                  className={`${BTN.secondary} disabled:cursor-not-allowed`}
                >
                  {dryRunSaving ? <span className="zi-spinner" aria-hidden="true" /> : null}
                  {dryRunSaving ? 'Starting in dry run…' : 'Start in dry-run instead'}
                </button>
              )}

              {confirmLive && dryRun ? (
                <button
                  type="button"
                  data-testid="cancel-turn-dry-run-off"
                  onClick={() => setConfirmLive(false)}
                  className={BTN.secondary}
                >
                  Keep dry run on
                </button>
              ) : null}
            </div>

            {dryRun === true ? (
              <p role="status" data-testid="step2-dry-run-state" className={`${stateBarClass('warn')} mt-4`}>
                <span>Dry run is on — this pass will classify everything and archive nothing.</span>
              </p>
            ) : dryRun === false ? (
              <p role="status" data-testid="step2-dry-run-state" className={`${stateBarClass('info')} mt-4`}>
                <span>
                  Dry run is off — this pass will really archive what it is confident about. Nothing
                  is ever deleted, and every action is undoable.
                </span>
              </p>
            ) : null}

            {dryRunError ? (
              <p role="alert" className={`${stateBarClass('danger')} mt-4`}>
                <span>Error — {dryRunError}</span>
                <button
                  type="button"
                  onClick={() => void setDryRun(!(dryRun ?? true))}
                  className={BTN.secondary}
                >
                  Retry
                </button>
              </p>
            ) : null}
          </section>
        ) : null}

        {step === 3 ? (
          <section aria-labelledby="zi-step3-heading" className="space-y-4">
            <h1 id="zi-step3-heading" className="zi-h1">
              Step 3 — Watch it work
            </h1>

            {/* The moment of trust. Real, or absent. */}
            {keepCallout ? (
              <p
                role="status"
                data-testid="never-miss-callout"
                className={stateBarClass('ok')}
              >
                <span>
                  Kept — “{keepCallout.subject}” — {keepCallout.reason}.
                </span>
              </p>
            ) : null}

            {/* The pinned line: what has and has not happened to your mail yet,
                AND the true reason why.

                Under dry run nothing is archived because dry run suppresses
                every mutation — not because the reviewer held it back. Saying
                "the reviewer checks every decision first" in that state
                attributes the outcome to the wrong cause, which is the one
                thing this product cannot afford to do. The line is derived from
                the real setting and is correct under BOTH values of dry_run. */}
            {dryRun === true ? (
              <p role="status" data-testid="dry-run-pinned-line" className={stateBarClass('warn')}>
                <span>
                  Dry run is on — nothing will be archived this pass. Every decision is recorded so
                  you can see exactly what would have happened.
                </span>
              </p>
            ) : archivedCount > 0 ? (
              <div role="status" data-testid="archiving-line" className={stateBarClass('info')}>
                <span>
                  Archiving now — <span className="zi-num">{archivedCount.toLocaleString()}</span> so
                  far. Undo any of it.
                </span>
                <button
                  type="button"
                  data-testid="undo-this-run"
                  onClick={() => void undoRun()}
                  disabled={undoing || !run}
                  title={
                    !run
                      ? 'There is no run to undo yet.'
                      : 'Restores every thread this run archived, from the snapshot taken before the change.'
                  }
                  className={`${BTN.secondary} disabled:cursor-not-allowed`}
                >
                  {undoing ? <span className="zi-spinner" aria-hidden="true" /> : null}
                  {undoing ? 'Undoing this run…' : 'Undo this run'}
                </button>
              </div>
            ) : (
              <p role="status" data-testid="nothing-archived-yet" className={stateBarClass('warn')}>
                <span>
                  Nothing has been archived yet — the reviewer checks every decision first.
                </span>
              </p>
            )}

            {undoResult ? (
              <p role="status" className={stateBarClass('ok')}>
                <span>Undone — {undoResult}</span>
              </p>
            ) : null}
            {undoError ? (
              <p role="alert" className={stateBarClass('danger')}>
                <span>Error — {undoError}</span>
              </p>
            ) : null}

            {/* The feed takes the viewport: no rail, no other card competing. */}
            <LiveRunFeed
              active={!runFinished}
              threadCount={run?.items_total ?? 0}
              onSeeAllActivity={openActivityDrawer}
              idleSummary={
                runFinished && run
                  ? `Last run: ${(run.counts?.applied ?? run.items_decided ?? 0).toLocaleString()} decisions · ${run.status}`
                  : null
              }
            />

            {runFinished ? (
              <div className="zi-card rounded-zi-r-lg p-6">
                <p className="zi-h2" data-testid="onboarding-complete">
                  That&rsquo;s the whole loop. From now on it runs on its own — you&rsquo;ll find
                  everything here.
                </p>
                <button
                  type="button"
                  onClick={onFinish}
                  className={`${BTN.primary} mt-4`}
                  data-testid="onboarding-finish"
                >
                  Go to your inbox
                </button>
              </div>
            ) : null}
          </section>
        ) : null}
      </main>
    </div>
  )
}
