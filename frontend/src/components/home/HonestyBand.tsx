'use client'

/**
 * The honesty band — spec/ui.md screen 19, "What it does when it can't be sure".
 *
 * This is the differentiator, and it is above the fold on desktop.
 *
 * ⚠️  THE NUMBERS BELOW ARE STATIC COPY. They describe one measured run on the
 * author's own inbox and are labelled as such. They are NEVER wired to an API
 * and NEVER presented as the visitor's own account — presenting a measured
 * result as a live stat would be exactly the kind of lie this band exists to
 * argue against. A generator must not fetch them.
 *
 * Prose, not a testimonial card, and no illustration.
 */

import { CONTENT } from '@/lib/tokens'

export function HonestyBand() {
  return (
    <section
      id="honesty"
      aria-labelledby="zi-honesty-heading"
      className="bg-zi-bg-subtle px-6 py-12 md:py-16"
    >
      <div className={`${CONTENT} max-w-3xl`}>
        <h2 id="zi-honesty-heading" className="zi-h1">
          What it does when it can&rsquo;t be sure
        </h2>

        <p className="zi-body-lg mt-6 text-zi-fg">
          On a real 2,122-thread inbox it archived <span className="zi-num">1,219</span> threads and
          took the inbox from <span className="zi-num">2,177</span> to{' '}
          <span className="zi-num">589</span>. Mid-run the model provider went down. It retried,
          switched models, and when every model failed it stopped and left{' '}
          <span className="zi-num">350</span> threads unread{' '}
          <strong className="font-semibold">and told the user so</strong> — rather than archiving
          mail it had never looked at.
        </p>

        <p className="zi-caption mt-6 text-zi-fg-muted" data-testid="honesty-provenance">
          Measured on the author&rsquo;s own inbox. These are one run&rsquo;s real numbers, not a
          projection and not your account&rsquo;s.
        </p>
      </div>
    </section>
  )
}
