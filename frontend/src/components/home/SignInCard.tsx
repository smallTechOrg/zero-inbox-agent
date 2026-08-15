'use client'

/**
 * Sign in / sign up — spec/ui.md screen 20.
 *
 * ONE screen, not two. There is no separate sign-up: the first successful
 * Google sign-in creates the account, and the copy says so rather than leaving
 * the visitor hunting for a "Create account" link that does not exist.
 *
 * The scope honesty is the point of this card, and it is REAL: `intent=signin`
 * requests `openid email profile` only (spec/api.md § Phase 8). Mailbox access
 * is a separate consent granted at onboarding step 1.
 */

import { SIGNIN_URL } from '@/lib/api'
import { BTN, CONTENT, stateBarClass } from '@/lib/tokens'

/** The `auth_error` query parameter the OAuth callback redirects back with. */
export const AUTH_ERROR_COPY: Record<string, { title: string; body: string }> = {
  auth_declined: {
    title: 'You cancelled sign-in.',
    body: 'Nothing was created and nothing was shared. You can try again whenever you like.',
  },
  access_denied: {
    title: 'You cancelled sign-in.',
    body: 'Nothing was created and nothing was shared. You can try again whenever you like.',
  },
  provider_error: {
    title: 'Google could not complete sign-in.',
    body: 'This is a problem at the provider, not with your account. Try again in a moment.',
  },
  validation_error: {
    title: 'That sign-in link was not valid.',
    body: 'The sign-in request expired or was tampered with. Start again from this page.',
  },
  mailbox_already_connected: {
    title: 'That mailbox is already connected to another Zero Inbox account.',
    body: 'Sign in as that account, or disconnect the mailbox there first.',
  },
}

function errorCopy(code: string) {
  return (
    AUTH_ERROR_COPY[code] ?? {
      title: 'Sign-in failed.',
      body: `The provider returned “${code}”. Nothing was created. Try again.`,
    }
  )
}

export function SignInCard({
  authError,
  onBack,
}: {
  /** The `?auth_error=` code from the OAuth callback, if any. */
  authError?: string | null
  onBack?: () => void
}) {
  const err = authError ? errorCopy(authError) : null

  return (
    <main id="main" className="bg-zi-bg px-6 py-12 md:py-16">
      <div className={`${CONTENT} max-w-xl`}>
        <section aria-labelledby="zi-signin-heading" className="zi-card rounded-zi-r-lg p-8">
          <h1 id="zi-signin-heading" className="zi-h1">
            Sign in to Zero Inbox
          </h1>

          <p className="zi-body mt-3 text-zi-fg-muted" data-testid="signin-creates-account">
            New here? Signing in creates your account.
          </p>

          {err ? (
            <div
              role="alert"
              data-testid="signin-error"
              className={`${stateBarClass('danger')} mt-6 block`}
            >
              <p className="zi-h3">Error — {err.title}</p>
              <p className="zi-body mt-1">{err.body}</p>
            </div>
          ) : null}

          <a
            href={SIGNIN_URL}
            data-testid="continue-with-google"
            className={`${BTN.primaryLarge} mt-6 w-full`}
          >
            Continue with Google
          </a>

          <div className="mt-6 rounded-zi-r-md border border-zi-border bg-zi-bg-subtle p-4">
            <h2 className="zi-h3">What Google is asked for right now</h2>
            <p className="zi-body mt-2 text-zi-fg-muted" data-testid="minimal-scope-promise">
              Signing in asks Google for your name and email address only. Access to your mail is a
              separate, later step you approve individually.
            </p>
          </div>

          {onBack ? (
            <button type="button" onClick={onBack} className="zi-link zi-body mt-6 inline-block">
              ← Back to the homepage
            </button>
          ) : null}
        </section>
      </div>
    </main>
  )
}
