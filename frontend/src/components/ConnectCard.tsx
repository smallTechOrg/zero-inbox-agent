'use client'

/**
 * Connect your mailbox — onboarding step 1 (spec/ui.md screen 21).
 *
 * ⚠️  THE COPY HERE IS LOAD-BEARING. The Phase-1 version of this card claimed
 * that nothing changed without the user's say-so, and that archiving would
 * never happen in Phase 1. Neither has been true since the agent started
 * archiving autonomously, and a first-run flow that overstates the safety
 * promise is worse than no flow at all. Both sentences are now gone from the
 * whole app, and Playwright asserts their absence.
 *
 * The replacement promise below is the one the code actually keeps.
 */

import { CONNECT_URL } from '@/lib/api'
import { BTN } from '@/lib/tokens'

const SCOPES: { label: string; why: string }[] = [
  {
    label: 'Read your mail headers, subjects and short snippets',
    why: 'So it can tell a newsletter from a person. Message bodies are never read or stored.',
  },
  {
    label: 'Archive and label mail',
    why: 'Archiving is how it clears your inbox. Labelling is how you can always find what it moved.',
  },
  {
    label: 'Create Gmail filters and draft replies',
    why: 'Used only when you ask for it — a proposed rule, or a draft you review before sending.',
  },
]

export function ConnectCard() {
  return (
    <section
      aria-labelledby="zi-connect-heading"
      className="zi-card mx-auto max-w-xl rounded-zi-r-lg p-6"
    >
      <h2 id="zi-connect-heading" className="zi-h1">
        Connect your mailbox
      </h2>

      <p className="zi-body mt-3 text-zi-fg-muted" data-testid="connect-promise">
        Zero Inbox reads your inbox threads, groups them into clusters and decides what is noise.{' '}
        <strong className="font-semibold text-zi-fg">
          It archives on its own once it&rsquo;s confident, and it labels everything it touches. It
          never deletes, and everything is undoable.
        </strong>
      </p>

      <div className="mt-6 rounded-zi-r-md border border-zi-border bg-zi-bg-subtle p-4">
        <h3 className="zi-caption tracking-wide text-zi-fg-muted uppercase">
          What we ask Google for, and why
        </h3>
        <ul className="mt-3 space-y-3">
          {SCOPES.map(scope => (
            <li key={scope.label}>
              <p className="zi-h3">{scope.label}</p>
              <p className="zi-body text-zi-fg-muted">{scope.why}</p>
            </li>
          ))}
        </ul>
      </div>

      <ul className="mt-6 space-y-2">
        <li className="zi-body text-zi-ok">
          Guaranteed — it never deletes anything. No trash, no spam-report, ever.
        </li>
        <li className="zi-body text-zi-ok">
          Guaranteed — every change is undoable, per action or for the whole run.
        </li>
        <li className="zi-body text-zi-ok">
          Guaranteed — People, Urgent and Legal mail, anyone you&rsquo;ve replied to, and anything it
          wasn&rsquo;t sure about all stay in your inbox.
        </li>
      </ul>

      <a href={CONNECT_URL} data-testid="connect-gmail" className={`${BTN.primaryLarge} mt-6`}>
        Connect Gmail
      </a>
    </section>
  )
}
