'use client'

/**
 * The hero — spec/ui.md screen 19.
 *
 * THERE IS EXACTLY ONE CALL TO ACTION ON THE HOMEPAGE, and it is the button
 * rendered here (`data-testid="primary-cta"`). Everything else that navigates
 * is a text link. A second competing button is a spec violation.
 */

import { BTN, CONTENT } from '@/lib/tokens'

export function Hero({ onSignIn }: { onSignIn: () => void }) {
  return (
    <section
      aria-labelledby="zi-hero-headline"
      className="bg-zi-bg-inverse px-6 py-12 text-white md:py-16"
    >
      <div className={CONTENT}>
        <h1 id="zi-hero-headline" className="zi-display max-w-3xl text-balance">
          An inbox that holds only what needs a human.
        </h1>

        <p className="zi-body-lg mt-6 max-w-2xl text-white/80">
          Zero Inbox reads your Gmail, decides what is noise, and archives it — never deletes it,
          always undoably, and never when it isn&rsquo;t sure.
        </p>

        <div className="mt-8 flex flex-wrap items-center gap-6">
          <button
            type="button"
            data-testid="primary-cta"
            onClick={onSignIn}
            className={`${BTN.primaryLarge} border-white bg-white text-zi-fg hover:bg-zi-bg-subtle`}
          >
            Sign in with Google
          </button>

          <a href="#safety-model" className="zi-link zi-body text-white/80 hover:text-white">
            How the safety model works ↓
          </a>
        </div>
      </div>
    </section>
  )
}
