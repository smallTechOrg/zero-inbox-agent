'use client'

/**
 * The hero and safety copy, readable with JavaScript disabled (spec/ui.md
 * screen 19: "Fully responsive; renders and is readable with JavaScript
 * disabled for the hero and safety copy.").
 *
 * The session gate is necessarily a client decision — we cannot know from a
 * static export whether this visitor has a cookie — so the prerendered HTML is
 * a neutral splash. This `<noscript>` block carries the front door's substance
 * for anyone who never runs the gate, and is inert for everyone who does. It is
 * NOT a stub: every word here is the real copy from `Hero` and `SafetyModel`.
 */

import { SAFETY_PROMISES } from './SafetyModel'

export function NoScriptFrontDoor() {
  return (
    <noscript>
      <div className="mx-auto max-w-2xl px-6 py-12">
        <h1 className="zi-display">An inbox that holds only what needs a human.</h1>
        <p className="zi-body-lg mt-6 text-zi-fg-muted">
          Zero Inbox reads your Gmail, decides what is noise, and archives it — never deletes it,
          always undoably, and never when it isn&rsquo;t sure.
        </p>

        <h2 className="zi-h1 mt-10">The safety model</h2>
        <ul className="mt-4 space-y-3">
          {SAFETY_PROMISES.map(promise => (
            <li key={promise} className="zi-body-lg">
              {promise}
            </li>
          ))}
        </ul>

        <p className="zi-body mt-10">
          <a href="/auth/google/start?intent=signin" className="zi-link">
            Sign in with Google
          </a>{' '}
          — signing in asks Google for your name and email address only. Access to your mail is a
          separate, later step you approve individually.
        </p>

        <p className="zi-caption mt-6 text-zi-fg-muted">
          JavaScript is disabled, so the live console cannot run — but nothing above depends on it.
        </p>
      </div>
    </noscript>
  )
}
