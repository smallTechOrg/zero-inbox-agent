'use client'

import { LOGIN_URL } from '../lib/api'

/**
 * The signed-out front door (spec/ui.md § Signed-out state): hero, one
 * "Sign in with Google" action, and the honesty band. No API calls, no
 * EventSource — a signed-out visitor never touches the backend.
 */
export function SignedOut() {
  return (
    <main className="mx-auto flex min-h-screen max-w-[var(--zi-content-max)] flex-col items-center justify-center gap-10 px-6 py-16 text-center">
      <div className="max-w-xl space-y-4">
        <h1 className="zi-display">Zero Inbox</h1>
        <p className="zi-body-lg text-zi-fg-muted">
          One button cleans your Gmail inbox. The agent files each thread into categories you
          control, narrates every action in plain English as it happens, and one click undoes an
          entire run.
        </p>
      </div>

      <a href={LOGIN_URL} className="zi-btn zi-btn-primary zi-btn-lg no-underline">
        Sign in with Google
      </a>

      {/* The honesty band — what the agent does and does not do. */}
      <ul className="zi-body grid max-w-2xl gap-2 rounded-zi-r-lg border border-zi-border bg-zi-bg-subtle px-6 py-5 text-left text-zi-fg-muted sm:grid-cols-3 sm:gap-4">
        <li>
          <strong className="text-zi-fg">Headers only.</strong> It reads senders, subjects and the
          short snippet — never an email body.
        </li>
        <li>
          <strong className="text-zi-fg">Everything is undoable.</strong> Every change is recorded
          and reversible with one click per run.
        </li>
        <li>
          <strong className="text-zi-fg">INBOX only.</strong> Archived mail is never read and never
          touched.
        </li>
      </ul>
    </main>
  )
}
