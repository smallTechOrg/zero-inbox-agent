'use client'

/**
 * The front door — spec/ui.md screens 19 and 20.
 *
 * This is what a visitor with no valid session sees at /app/. It is a full
 * replacement for the old behaviour, where an unauthenticated visitor landed in
 * an empty operator console and had to guess what the product was. The console
 * is never rendered here — not skeletally, not for a frame.
 *
 * Rendering rules held here:
 *  · exactly ONE call to action (the hero button). The top-bar "Sign in" is a
 *    text link and the safety anchor is a text link.
 *  · no left rail, no run-status pill, no cluster list, no dry-run banner.
 *  · the hero and safety copy are plain server-rendered markup — they read fine
 *    with JavaScript disabled.
 */

import { useCallback, useEffect, useState } from 'react'
import { CONTENT, stateBarClass } from '@/lib/tokens'
import { Hero } from './Hero'
import { HonestyBand } from './HonestyBand'
import { HowItWorks } from './HowItWorks'
import { SafetyModel } from './SafetyModel'
import { SignInCard } from './SignInCard'
import { SiteFooter } from './SiteFooter'

export type HomepageProps = {
  /** Set when an authenticated session disappeared mid-use (spec/ui.md § States):
   *  the user gets one plain sentence, never a wall of failed panels. */
  signedOutNotice?: boolean
}

function readInitialView(): { view: 'home' | 'signin'; authError: string | null } {
  if (typeof window === 'undefined') return { view: 'home', authError: null }
  const params = new URLSearchParams(window.location.search)
  const authError = params.get('auth_error')
  const wantsSignin = params.get('view') === 'signin' || window.location.hash === '#signin'
  return { view: authError || wantsSignin ? 'signin' : 'home', authError }
}

export function Homepage({ signedOutNotice = false }: HomepageProps) {
  const [view, setView] = useState<'home' | 'signin'>('home')
  const [authError, setAuthError] = useState<string | null>(null)

  // Read after mount so the static export's first paint is identical for
  // everyone (no hydration mismatch), then switch if the URL asked for sign-in.
  useEffect(() => {
    const initial = readInitialView()
    setView(initial.view)
    setAuthError(initial.authError)
  }, [])

  const goSignIn = useCallback(() => {
    setView('signin')
    if (typeof window !== 'undefined') window.history.replaceState(null, '', '/app/?view=signin')
  }, [])

  const goHome = useCallback(() => {
    setView('home')
    setAuthError(null)
    if (typeof window !== 'undefined') window.history.replaceState(null, '', '/app/')
  }, [])

  return (
    <div data-testid="homepage" className="flex min-h-screen flex-col bg-zi-bg text-zi-fg">
      <header className="border-b border-zi-border bg-zi-bg">
        <div className={`${CONTENT} flex items-center justify-between gap-4 px-6 py-4`}>
          <a
            href="/app/"
            onClick={e => {
              e.preventDefault()
              goHome()
            }}
            className="zi-h2 zi-focusable rounded-zi-r-sm no-underline"
          >
            Zero Inbox
          </a>

          {view === 'home' ? (
            /* A text LINK, not a button — the hero CTA is the page's only
               call to action and must not have a competitor. */
            <a
              href="/app/?view=signin"
              data-testid="topbar-signin"
              onClick={e => {
                e.preventDefault()
                goSignIn()
              }}
              className="zi-link zi-body"
            >
              Sign in
            </a>
          ) : null}
        </div>
      </header>

      {signedOutNotice ? (
        <div className={`${CONTENT} px-6 pt-6`}>
          <p role="status" data-testid="signed-out-notice" className={stateBarClass('warn')}>
            <span>Signed out — You were signed out.</span>
          </p>
        </div>
      ) : null}

      {view === 'signin' ? (
        <SignInCard authError={authError} onBack={goHome} />
      ) : (
        <main id="main" className="flex-1">
          <Hero onSignIn={goSignIn} />
          <HonestyBand />
          <HowItWorks />
          <SafetyModel />
        </main>
      )}

      <SiteFooter />
    </div>
  )
}
